"""Anthropic provider. Structured output via forced tool use."""

import json
from typing import TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from repopilot.llm.base import LLMError
from repopilot.observability import get_logger

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 4096) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        """Force the model to answer by 'calling' a tool whose input schema is our model.

        This is the reliable way to get JSON: the API validates the shape server-side,
        so we don't parse prose or strip ```json fences.
        """
        tool_name = _snake(schema.__name__)
        json_schema = schema.model_json_schema()

        response = await self._client.messages.create(
            model=self._model,
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


def _snake(camel: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(camel):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)
