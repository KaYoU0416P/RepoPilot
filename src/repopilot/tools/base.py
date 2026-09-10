"""Tool contract + registry.

Every tool is an async callable taking (Workspace, **kwargs) and returning ToolResult.
The registry adds the cross-cutting concerns the agent must not re-implement per tool:
concurrency cap, timeout, error capture, timing.
"""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from repopilot.observability import get_logger
from repopilot.workspace import PathEscapeError, Workspace

log = get_logger(__name__)

#: 审计流。单独一个 logger name，因为它和调试日志的**受众不同**：
#: 调试日志出事时才看，审计日志是「Agent 动过什么」的账本，要能单独路由、
#: 单独保留、单独调级别，而不用把整个 DEBUG 打开。
#: run_id 不用手工拼 —— `observability/logging.py` 的 `_RunIdFilter` 从
#: ContextVar 里取，每条日志自带 `[run=…]`。
audit = get_logger("repopilot.audit")

Risk = Literal["read", "write", "execute"]

#: 要写审计的风险等级。`read` 不记：一次 run 几十次读，记了等于没记，
#: 真正需要事后追责的是「改了什么」和「跑了什么」。
_AUDITED: frozenset[str] = frozenset({"write", "execute"})


class ToolResult(BaseModel):
    """Uniform envelope so a node never has to branch on tool identity."""

    tool: str
    ok: bool
    content: str = ""
    error: str | None = None
    duration_ms: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class ToolFn(Protocol):
    async def __call__(self, workspace: Workspace, /, **kwargs: Any) -> ToolResult: ...


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    risk: Risk
    fn: ToolFn
    parameters: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Holds tools and enforces the runtime guardrails around them."""

    def __init__(self, *, timeout: float = 20.0, max_concurrency: int = 4) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._timeout = timeout
        # Semaphore == a permit counter. Bounds how many tool coroutines may be
        # in flight at once, so a 40-file plan cannot open 40 subprocesses.
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    async def call(
        self,
        name: str,
        workspace: Workspace,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(tool=name, ok=False, error=f"unknown tool: {name}")

        started = time.perf_counter()
        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    spec.fn(workspace, **kwargs),
                    timeout=timeout or self._timeout,
                )
            except TimeoutError:
                result = ToolResult(tool=name, ok=False, error="tool timed out")
            except PathEscapeError as exc:
                result = ToolResult(tool=name, ok=False, error=f"blocked by sandbox: {exc}")
            except NotImplementedError as exc:
                result = ToolResult(tool=name, ok=False, error=f"not implemented: {exc}")
            except TypeError as exc:
                result = ToolResult(tool=name, ok=False, error=f"bad arguments: {exc}")
            except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the run
                result = ToolResult(tool=name, ok=False, error=f"{type(exc).__name__}: {exc}")

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info("tool=%s ok=%s %dms", name, result.ok, result.duration_ms)

        if spec.risk in _AUDITED:
            audit.info(
                "risk=%s tool=%s ok=%s %dms %s%s",
                spec.risk,
                name,
                result.ok,
                result.duration_ms,
                _audit_args(kwargs),
                f" error={result.error}" if result.error else "",
            )
        return result


#: 超过这个长度的参数值不进日志，只留长度 + 摘要。
_AUDIT_VALUE_CHARS = 80


def _audit_args(kwargs: dict[str, Any]) -> str:
    """把工具参数压成一行审计文本。长值只留 `sha256`，不留原文。

    两个理由：
      1. `write_file` 的 `content` 是整份文件。原样进日志，日志体积会跟着
         被写文件走，而且日志本身就成了一条数据外泄通道。
      2. 追责要的是「动了哪个文件、内容指纹是什么」，不是内容本身 ——
         内容在 git diff 里，PR 上看得见。摘要负责把两者对上号。

    **已知取舍**：只在调用**结束后**记一条。进程被 SIGKILL 打死在工具执行中间，
    这一条就没了（超时和异常不受影响，那两条路都会走到这里）。要补的话得在
    调用前再记一条 intent，代价是审计量翻倍。
    """
    parts = []
    for key, value in kwargs.items():
        text = str(value)
        if len(text) <= _AUDIT_VALUE_CHARS:
            parts.append(f"{key}={text!r}")
        else:
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
            parts.append(f"{key}=<{len(text)}chars sha256:{digest}>")
    return " ".join(parts)


def tool(
    name: str, description: str, risk: Risk, parameters: dict[str, Any] | None = None
) -> Callable[[Callable[..., Awaitable[ToolResult]]], Callable[..., Awaitable[ToolResult]]]:
    """Decorator that tags a coroutine with its ToolSpec (attached, not registered)."""

    def decorator(fn: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
        fn.__tool_spec__ = ToolSpec(  # type: ignore[attr-defined]
            name=name,
            description=description,
            risk=risk,
            fn=fn,  # type: ignore[arg-type]
            parameters=parameters or {},
        )
        return fn

    return decorator
