"""Anthropic provider. Structured output via forced tool use.

**为什么这里没有开 prompt caching（`cache_control`）** —— 算过了，开了是纯亏：

  1. 缓存是**前缀匹配**，而请求的渲染顺序是 `tools` → `system` → `messages`。
     想缓存 SYSTEM，被缓存的前缀其实是 `tools + system`。
  2. 我们的 `tools` 里装的是 Analysis / Plan / EditSet 的 JSON Schema ——
     **三个节点各不相同**。所以三次调用压根没有共享前缀，
     只有 execute 重试时前缀才重复。
  3. 就算重复了也不够长：Sonnet 4.6 的最小可缓存前缀是 **2048 token**，
     而 SYSTEM 只有 ~959 字符 ≈ 240 token。**低于下限不会报错，只是静默不缓存**，
     而写缓存要按 1.25 倍计费 —— 加了等于白付 25%，一次都读不回来。

所以这里的选择是**先量再说**：`Usage.cache_read_input_tokens` 和
`cache_hit_rate` 已经接进报表了。真要用上缓存，得先让稳定前缀超过 2048 token
（比如把大段仓库上下文钉在 SYSTEM 里），那时候数字会自己说话。
"""

import json
from typing import TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from repopilot.llm.base import LLMError
from repopilot.llm.usage import Usage
from repopilot.observability import get_logger

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 4096) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self._max_tokens = max_tokens
        #: 这个实例（= 这个 run）的累计用量。见 `LLMClient.usage` 的前提说明。
        self.usage = Usage()

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        """Force the model to answer by 'calling' a tool whose input schema is our model.

        This is the reliable way to get JSON: the API validates the shape server-side,
        so we don't parse prose or strip ```json fences.
        """
        tool_name = _snake(schema.__name__)
        json_schema = schema.model_json_schema()

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": schema.__doc__ or f"Return a {schema.__name__}",
                    "input_schema": json_schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )

        # 计量放在解析**之前**：schema 校验失败照样是花了钱的。
        # 只在成功路径上记账，等于给自己发了一张少算的账单。
        self.usage = self.usage + _usage_of(response)

        for block in response.content:
            if block.type == "tool_use":
                try:
                    return schema.model_validate(block.input)
                except ValidationError as exc:
                    got = json.dumps(block.input)[:500]
                    raise LLMError(
                        f"{schema.__name__} validation failed: {exc}\ngot: {got}"
                    ) from exc

        raise LLMError(f"model returned no tool_use block (stop_reason={response.stop_reason})")


def _usage_of(response) -> Usage:
    """从响应里取四个 token 字段。

    `getattr(..., 0)` 不是防御性编程洁癖：两个 cache 字段只有在请求真的带了
    `cache_control` 时才有值，不同 SDK 版本上它们可能是 `None` 或干脆缺席。
    这里把缺失一律归零，免得计量本身把整个 run 带崩 —— **记账不该比业务还脆**。
    """
    raw = getattr(response, "usage", None)
    if raw is None:
        return Usage(calls=1)
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        calls=1,
    )


def _snake(camel: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(camel):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)
