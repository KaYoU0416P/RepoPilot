from repopilot.config import get_settings
from repopilot.llm.base import LLMClient, LLMError
from repopilot.llm.budget import BudgetedLLM, BudgetExceeded, budget_from_settings
from repopilot.llm.scripted import ScriptedLLM


def build_llm() -> LLMClient:
    """Provider selection lives here so nodes only ever see the LLMClient protocol.

    最后统一套一层 `BudgetedLLM` —— **熔断只写一遍，所有 provider 都有**。
    这是单方法协议的第四次兑现（前三次：加计量、加 span、换供应商）。
    """
    return BudgetedLLM(_provider(), budget_from_settings())


def _provider() -> LLMClient:
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        from repopilot.llm.anthropic_client import AnthropicLLM

        return AnthropicLLM(
            api_key=settings.anthropic_api_key,
            model=settings.model,
            max_tokens=settings.max_tokens,
        )
    if settings.llm_provider == "deepseek":
        from repopilot.llm.deepseek_client import DeepSeekLLM

        return DeepSeekLLM(
            api_key=settings.deepseek_api_key,
            model=settings.model,
            max_tokens=settings.max_tokens,
            base_url=settings.deepseek_base_url,
        )
    return ScriptedLLM()


__all__ = ["BudgetExceeded", "LLMClient", "LLMError", "ScriptedLLM", "build_llm"]
