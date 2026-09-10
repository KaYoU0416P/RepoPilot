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

**2b. ★但工具「强制」不了 —— 这是一处真实的能力缺口。**
DeepSeek V4 的两个模型都**常驻思考模式**，而思考模式不接受任何形式的强制：

    tool_choice={"type":"function",...}  → 400 Thinking mode does not support this tool_choice
    tool_choice="required"               → 400（同上）
    tool_choice="auto"                   → 200，且实测确实调了工具
    （不传）                              → 200，同上

（上面四行是花两分钱真打出来的，不是查文档猜的。这是上游的已知限制，
有公开 issue，各家 Agent 框架都得给 V4 关掉 `supportsToolChoice`。）

所以这里只能用 `auto` + **一个**工具 + SYSTEM 里明说「用工具回答」。
差别要讲清楚：**Anthropic 那条路，"必须返回结构化输出"是协议保证的；
这条路只是"极可能"。** 模型有权改口说人话。兜底见 `_tool_arguments`。

**3. 缓存是自动的，不用发 `cache_control`。**
于是 `Usage.cache_read_input_tokens` 在这里会**真的有非零值** ——
`anthropic_client.py` 里那段「算完决定不开 prompt caching」的结论只对 Anthropic 成立。
当初埋的 `cache_hit_rate` 指标到这里才第一次派上用场。
"""

import copy
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
        tool_choice: str = "auto",
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._base_url = base_url.rstrip("/")
        #: 默认 `"auto"`：DeepSeek V4 常驻思考模式，强制会 400（见模块头注释 2b）。
        #: 留成参数是因为**别的 OpenAI 兼容端点未必有这个限制** —— 指向一个
        #: 非思考模型时传 `"required"` 就能把强制拿回来。
        self._tool_choice = tool_choice
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
                        # ★不能直接送 `model_json_schema()`，strict 模式对形状
                        # 另有要求，送原样会被 400 拒。见 `strictify`。
                        "parameters": strictify(schema.model_json_schema()),
                        # ★没有它，arguments 只是「尽量」符合 schema
                        "strict": True,
                    },
                }
            ],
            # 只挂**一个**工具。强制不了的时候，"只有一把锤子"就是让模型
            # 用它的最强手段 —— 挂两个工具会立刻多出一个"选错"的失败模式。
            "tool_choice": self._tool_choice,
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
                reason = _finish_reason(body)
                # `length` 值得单独说人话：它不是"模型不听话"，是**输出预算烧完了**。
                # 思考模型的思考过程本身就吃 output token，回复被从中间截断，
                # 于是既没有 tool_call 也没有能解析的正文。报错不指出这一点的话，
                # 人会去调 prompt —— 而真正该调的是 max_tokens（或者加成本熔断）。
                if reason == "length":
                    raise LLMError(
                        f"回复被 max_tokens={self._max_tokens} 截断（finish_reason=length），"
                        f"本次已产出 {call.output_tokens} 个 output token。"
                        f"思考模型的思考过程也吃 output 预算，"
                        f"调大 REPOPILOT_MAX_TOKENS 或换非思考模型。"
                    )
                raise LLMError(
                    f"model returned neither a tool_call nor parseable JSON "
                    f"(finish_reason={reason})"
                )
            try:
                return schema.model_validate(arguments)
            except ValidationError as exc:
                got = json.dumps(arguments)[:500]
                raise LLMError(
                    f"{schema.__name__} validation failed: {exc}\ngot: {got}"
                ) from exc


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """把 pydantic 生成的 JSON Schema 改造成 `strict` 模式能接受的形状。

    ★这是冒烟测试第一次真调 API 就撞上的 400：

        {"error":{"message":"Required properties must match all properties in the object"}}

    strict 模式对 schema 有两条额外要求，pydantic 两条都不满足：

      1. **`required` 必须列出全部属性。** pydantic 只把「没有默认值」的字段
         列进 `required` —— `EditSet.rationale` 有 `default=""`，于是它不在
         `required` 里，schema 就被判非法。
      2. **每个 object 都要 `additionalProperties: false`。** pydantic 不写这一项。

    两条都要**递归**处理：`EditSet` 的 `$defs.FileEdit` 是嵌套 object，
    只改顶层照样被拒。

    **一个真实的语义损失，要主动讲**：strict 模式下 `required` 的含义从
    「业务上必填」变成「模型必须输出这个键」。于是 pydantic 那边的 `default`
    **永远用不上了** —— 模型每次都会给一个值。对我们这几个 schema 无害
    （默认值都是空字符串 / 空列表），但这是**用表达力换确定性**：
    schema 不再能表达「这个字段可省略」。

    纯函数 + 深拷贝：不改调用方传进来的那份，schema 在别处还要用。
    """
    out = copy.deepcopy(schema)
    _strictify_in_place(out)
    return out


def _strictify_in_place(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _strictify_in_place(item)
        return
    if not isinstance(node, dict):
        return

    properties = node.get("properties")
    if isinstance(properties, dict):
        node["required"] = list(properties)
        node["additionalProperties"] = False

    for value in node.values():
        _strictify_in_place(value)


def _tool_arguments(body: dict[str, Any]) -> dict[str, Any] | None:
    """把模型这次的结构化输出挖出来。先走 tool_call，再退到正文里捞 JSON。

    ★`arguments` 是一个**字符串**，不是对象 —— OpenAI 协议在这里和 Anthropic
    不一样（那边 `block.input` 直接就是 dict）。忘了这一步会得到一个
    很难看懂的 pydantic 报错：「期望 object，得到 str」。

    **为什么需要正文兜底**：强制不了工具（见模块头注释 2b），模型有权
    改口说人话。真发生时它通常还是吐一份 JSON，只是没包在 tool_call 里。
    与其让整个 run 死在 `analyze` 节点上，不如把这种情况捞回来 ——
    **反正 pydantic 那一关照样要过，捞错了会在下一步被挡住，不会放行脏数据。**

    诚实地说：这一层兜底存在本身就是「这条路的结构化输出只是极可能，
    不是协议保证」的证据。Anthropic 那条路不需要它。
    """
    choices = body.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}

    calls = message.get("tool_calls") or []
    if calls:
        raw = (calls[0].get("function") or {}).get("arguments")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LLMError(f"tool_call arguments 不是合法 JSON: {raw[:300]}") from exc

    return _json_from_prose(message.get("content") or "")


def _json_from_prose(content: str) -> dict[str, Any] | None:
    """从自由文本里捞出一个 JSON 对象。捞不到就 `None`，不抛。

    只做最省事的两步：剥 ```json 围栏，然后取第一个 `{` 到最后一个 `}`。
    刻意**不**做括号配平之类的花活 —— 这是兜底路径，值得的复杂度有限，
    捞不干净就让它失败，失败是看得见的。
    """
    text = content.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
