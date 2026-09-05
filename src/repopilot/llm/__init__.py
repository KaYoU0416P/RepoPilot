from repopilot.config import get_settings
from repopilot.llm.base import LLMClient, LLMError
from repopilot.llm.scripted import ScriptedLLM


def build_llm() -> LLMClient:
    """Provider selection lives here so nodes only ever see the LLMClient protocol."""
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        from repopilot.llm.anthropic_client import AnthropicLLM

        return AnthropicLLM(
            api_key=settings.anthropic_api_key,
            model=settings.model,
            max_tokens=settings.max_tokens,
        )
    return ScriptedLLM()


__all__ = ["LLMClient", "LLMError", "ScriptedLLM", "build_llm"]
