"""DeepSeek provider。OpenAI 兼容协议，结构化输出同样靠强制 tool use。

**为什么手写 httpx 而不是引 `openai` SDK**：我们只用一个端点
（`POST /chat/completions`）、一种用法（强制调用一个工具）。
和 `github/client.py` 不引 PyGithub、`mcp/protocol.py` 不引 MCP SDK 是同一个判断：
一层薄封装比一个几万行的依赖更好交代，而且 httpx 本来就在依赖里了。

**这个类不止能接 DeepSeek。** OpenAI 兼容层是国产模型的事实标准 ——
换个 `base_url` + `model` 就能接 Qwen / Kimi / GLM。所以 `base_url` 是构造参数
而不是常量。

## 和 Anthropic 那版的三处真实差异

**1. `tool_choice` 的形状不同**（语义相同：强制模型必须调这个工具）::

    {"type": "function", "function": {"name": …}}   # 这里
    {"type": "tool", "name": …}                     # Anthropic

**2. schema 遵循要开 `strict`，而 `strict` 在 beta 通道。**
不开的话 arguments 只是「尽量」符合 schema，pydantic 校验会抛 `LLMError`。
所以默认 `base_url` 指向 `/beta` —— 这不是尝鲜，是**把服务端校验拿回来**。
Anthropic 那边强制 tool use 天然就是服务端校验的，这里要显式换来。

**3. 缓存是自动的，不用发 `cache_control`。**
于是 `Usage.cache_read_input_tokens` 在这里会**真的有非零值** ——
`anthropic_client.py` 里那段「算完决定不开 prompt caching」的结论只对 Anthropic 成立。
当初埋的 `cache_hit_rate` 指标到这里才第一次派上用场。
"""

import json
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from repopilot.llm.base import LLMError
from repopilot.llm.usage import Usage
from repopilot.observability import get_logger, set_attrs, span

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

#: 默认走 beta 通道 —— `strict` 模式只在这里有。见模块头注释第 2 点。
DEFAULT_BASE_URL = "https://api.deepseek.com/beta"


class DeepSeekLLM:
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._base_url = base_url.rstrip("/")
        # 和 GitHubClient 一样留一个注入点：测试塞 MockTransport，不联网不花钱。
        self._client = client or httpx.AsyncClient(timeout=timeout)
        #: 这个实例（= 这个 run）的累计用量。见 `LLMClient.usage` 的前提说明。
        self.usage = Usage()

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        """强制模型「调用」一个入参 schema 就是我们模型的工具，以此拿到结构化输出。"""
        tool_name = _snake(schema.__name__)

        payload = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": schema.__doc__ or f"Return a {schema.__name__}",
                        "parameters": schema.model_json_schema(),
                        # ★没有它，arguments 只是「尽量」符合 schema
                        "strict": True,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        }

        attrs = {"llm.model": self.model, "llm.schema": schema.__name__, "llm.provider": "deepseek"}
        with span("llm.structured", **attrs) as current:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            if response.status_code >= 400:
                # 只记 url 和状态码，body 可能回显请求内容 —— 而请求里有 Issue 正文。
                log.warning("DeepSeek %s -> %s", self.model, response.status_code)
                raise LLMError(f"DeepSeek {response.status_code}: {response.text[:300]}")

            body = response.json()

            # 计量放在解析**之前**：schema 校验失败照样是花了钱的。
            call = _usage_of(body)
            self.usage = self.usage + call
            set_attrs(
                current,
                **{
                    "llm.input_tokens": call.total_input_tokens,
                    "llm.output_tokens": call.output_tokens,
                    "llm.cache_read_tokens": call.cache_read_input_tokens,
                    "llm.finish_reason": _finish_reason(body),
                },
            )

            arguments = _tool_arguments(body)
            if arguments is None:
                raise LLMError(
                    f"model returned no tool_call (finish_reason={_finish_reason(body)})"
                )
            try:
                return schema.model_validate(arguments)
            except ValidationError as exc:
                got = json.dumps(arguments)[:500]
                raise LLMError(
                    f"{schema.__name__} validation failed: {exc}\ngot: {got}"
                ) from exc


def _tool_arguments(body: dict[str, Any]) -> dict[str, Any] | None:
    """把 `choices[0].message.tool_calls[0].function.arguments` 挖出来。

    ★`arguments` 是一个**字符串**，不是对象 —— OpenAI 协议在这里和 Anthropic
    不一样（那边 `block.input` 直接就是 dict）。忘了这一步会得到一个
    很难看懂的 pydantic 报错：「期望 object，得到 str」。
    """
    choices = body.get("choices") or []
    if not choices:
        return None
    calls = (choices[0].get("message") or {}).get("tool_calls") or []
    if not calls:
        return None
    raw = (calls[0].get("function") or {}).get("arguments")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"tool_call arguments 不是合法 JSON: {raw[:300]}") from exc


def _finish_reason(body: dict[str, Any]) -> str | None:
    choices = body.get("choices") or []
    return choices[0].get("finish_reason") if choices else None


def _usage_of(body: dict[str, Any]) -> Usage:
    """把 OpenAI 语义的 usage 映射成我们这套（Anthropic 语义的）四个桶。

    这个映射**不是逐字段改名**，有两处要想清楚：

      * `prompt_tokens` 是**输入总量**（命中 + 未命中），而我们的
        `input_tokens` 只装「未命中缓存」那部分 —— 直接映射会把缓存那部分
        重复计一遍。所以取 `prompt_cache_miss_tokens`，缺失时才回退到
        `prompt_tokens - cache_hit`。
      * `cache_creation_input_tokens` 恒为 0：DeepSeek 的缓存是自动的，
        **不额外收写入费**，没有对应的桶。这里留 0 而不是瞎填，
        成本公式里那一项自然就是 0。
    """
    raw = body.get("usage") or {}
    if not raw:
        return Usage(calls=1)

    hit = raw.get("prompt_cache_hit_tokens") or 0
    miss = raw.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max((raw.get("prompt_tokens") or 0) - hit, 0)

    return Usage(
        input_tokens=miss,
        output_tokens=raw.get("completion_tokens") or 0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=hit,
        calls=1,
    )


def _snake(camel: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(camel):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)
