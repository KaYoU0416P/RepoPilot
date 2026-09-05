"""Tool contract + registry.

Every tool is an async callable taking (Workspace, **kwargs) and returning ToolResult.
The registry adds the cross-cutting concerns the agent must not re-implement per tool:
concurrency cap, timeout, error capture, timing.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from repopilot.observability import get_logger
from repopilot.workspace import PathEscapeError, Workspace

log = get_logger(__name__)

Risk = Literal["read", "write", "execute"]


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
        return result


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
