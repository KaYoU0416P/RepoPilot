"""LLM boundary.

One method: given a system prompt, a user prompt and a Pydantic model, return a
validated instance of that model. Structured output everywhere means nodes never
parse free text, and swapping providers cannot change the graph.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """Structural typing: any class with this method satisfies it, no `implements`."""

    name: str

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T: ...


class LLMError(RuntimeError):
    pass
