"""LLM boundary.

One method: given a system prompt, a user prompt and a Pydantic model, return a
validated instance of that model. Structured output everywhere means nodes never
parse free text, and swapping providers cannot change the graph.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from repopilot.llm.usage import Usage

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """Structural typing: any class with this method satisfies it, no `implements`."""

    name: str
    #: 模型标识。成本要按模型算，所以这个必须能从外面读到。
    model: str
    #: 这个客户端实例迄今为止的累计用量。
    #:
    #: **前提：一个 run 一个客户端实例**（`build_llm()` 每次都新建，
    #: `runner.py` / `harness.py` 每个 run 各调一次）。所以「实例累计」
    #: 就等于「这个 run 的总量」，节点不用自己做差。
    #: 这条前提有测试钉着 —— 哪天有人给 `build_llm` 加了缓存，测试会红。
    usage: Usage

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T: ...


class LLMError(RuntimeError):
    pass
