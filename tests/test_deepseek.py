"""DeepSeek provider 的验收测试。不联网、不花 token。

外部依赖切在 `httpx.MockTransport` 上 —— 和 `test_publishing.py` 对 GitHub API
的做法一致：**HTTP 那一层是假的，我们自己的解析/映射/计量全是真的跑了一遍**。
"""

import json

import httpx
import pytest

from repopilot.agent.schemas import TestOutcome
from repopilot.config import DEFAULT_MODELS, Settings, get_settings
from repopilot.llm.base import LLMError
from repopilot.llm.deepseek_client import DeepSeekLLM, _usage_of, strictify


def _body(arguments: dict, usage: dict | None = None, finish: str = "tool_calls") -> dict:
    """一份最小的 OpenAI 兼容响应。"""
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "test_outcome",
                                # ★arguments 是**字符串**，不是对象
                                "arguments": json.dumps(arguments),
                            }
                        }
                    ]
                },
            }
        ],
        "usage": usage if usage is not None else {},
    }


def _llm(handler, **kwargs) -> DeepSeekLLM:
    transport = httpx.MockTransport(handler)
    return DeepSeekLLM(
        api_key="dummy",
        model="deepseek-v4-pro",
        client=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


# ------------------------------------------------------------ 请求长什么样
async def test_exactly_one_tool_is_offered_and_it_is_our_schema():
    """强制不了工具（思考模式限制），所以"只挂一把锤子"就是让模型用它的最强手段。

    挂两个工具会立刻多出一个「选错」的失败模式，而我们没有 tool_choice 兜底。
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_body({"passed": True, "output": "ok"}))

    await _llm(handler).structured(system="s", user="u", schema=TestOutcome)

    (tool,) = seen["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "test_outcome"
    # ★没有 strict，arguments 只是"尽量"符合 schema，服务端校验就丢了
    assert tool["function"]["strict"] is True
    # 送的是**改造过**的 schema，不是 pydantic 原样吐出来的那份（见 strictify）
    assert tool["function"]["parameters"] == strictify(TestOutcome.model_json_schema())


async def test_tool_choice_defaults_to_auto_because_forcing_is_a_400():
    """★实测出来的约束，不是查文档猜的。

    DeepSeek V4 常驻思考模式，`{"type":"function",...}` 和 `"required"` 都会
    换回 400「Thinking mode does not support this tool_choice」。只有 auto 能用。
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_body({"passed": True, "output": "ok"}))

    await _llm(handler).structured(system="s", user="u", schema=TestOutcome)
    assert seen["tool_choice"] == "auto"


async def test_tool_choice_stays_configurable_for_other_compatible_endpoints():
    """别的 OpenAI 兼容端点未必有这个限制 —— 指向非思考模型时要能把强制拿回来。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_body({"passed": True, "output": "ok"}))

    llm = _llm(handler, tool_choice="required")
    await llm.structured(system="s", user="u", schema=TestOutcome)
    assert seen["tool_choice"] == "required"


async def test_system_and_user_become_two_messages_not_a_system_field():
    """OpenAI 协议没有独立的 `system` 字段，system 是 messages 里的一条。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_body({"passed": True, "output": "ok"}))

    await _llm(handler).structured(system="SYS", user="USR", schema=TestOutcome)

    assert seen["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]


async def test_default_base_url_is_the_beta_channel():
    """`strict` 只在 beta 通道上有。默认走那里不是尝鲜，是把服务端校验拿回来。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_body({"passed": True, "output": "ok"}))

    await _llm(handler).structured(system="s", user="u", schema=TestOutcome)
    assert seen["url"] == "https://api.deepseek.com/beta/chat/completions"


# ------------------------------------------------------- strict schema 改造
def test_every_property_becomes_required():
    """★这条钉的是冒烟测试真撞到的 400。

    pydantic 只把「没有默认值」的字段列进 `required`，而 strict 模式要求
    列出**全部**属性，否则直接 400：
    「Required properties must match all properties in the object」。
    """
    from repopilot.agent.schemas import Analysis

    strict = strictify(Analysis.model_json_schema())
    assert set(strict["required"]) == set(strict["properties"])
    # 原样的 schema 里 relevant_files 有 default_factory，所以不在 required
    assert "relevant_files" not in Analysis.model_json_schema()["required"]


def test_nested_definitions_are_strictified_too():
    """`EditSet.$defs.FileEdit` 是嵌套 object，只改顶层照样被拒。"""
    from repopilot.agent.schemas import EditSet

    strict = strictify(EditSet.model_json_schema())
    nested = strict["$defs"]["FileEdit"]
    assert set(nested["required"]) == {"path", "content", "rationale"}
    assert nested["additionalProperties"] is False


def test_additional_properties_is_closed_everywhere():
    from repopilot.agent.schemas import EditSet

    strict = strictify(EditSet.model_json_schema())
    assert strict["additionalProperties"] is False
    assert strict["$defs"]["FileEdit"]["additionalProperties"] is False


def test_strictify_does_not_mutate_its_input():
    """schema 在别处还要用（比如 Anthropic 那条路），不能就地改。"""
    from repopilot.agent.schemas import Analysis

    original = Analysis.model_json_schema()
    before = json.dumps(original, sort_keys=True)
    strictify(original)
    assert json.dumps(original, sort_keys=True) == before


# --------------------------------------------------------------- 解析响应
async def test_arguments_is_a_json_string_and_gets_parsed():
    """★和 Anthropic 的真实差异：那边 `block.input` 直接是 dict，这边是字符串。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body({"passed": False, "output": "boom"}))

    result = await _llm(handler).structured(system="s", user="u", schema=TestOutcome)
    assert isinstance(result, TestOutcome)
    assert result.passed is False
    assert result.output == "boom"


def _prose(content: str) -> dict:
    """模型没调工具，改口说人话了。"""
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


async def test_a_response_with_neither_tool_call_nor_json_is_an_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_prose("我觉得这个任务不太清楚，能再说说吗？"))

    with pytest.raises(LLMError, match="neither a tool_call nor parseable JSON"):
        await _llm(handler).structured(system="s", user="u", schema=TestOutcome)


@pytest.mark.parametrize(
    "content",
    [
        '{"passed": true, "output": "ok"}',
        '```json\n{"passed": true, "output": "ok"}\n```',
        '好的，结果如下：\n{"passed": true, "output": "ok"}\n希望有帮助。',
    ],
)
async def test_json_in_the_prose_is_recovered_when_the_model_skips_the_tool(content):
    """★兜底路径。强制不了工具，模型有权改口说人话。

    真发生时它通常还是吐一份 JSON，只是没包在 tool_call 里。与其让整个 run
    死在 analyze 节点上，不如捞回来 —— pydantic 那一关照样要过，
    捞错了会在下一步被挡住，不会放行脏数据。

    这层兜底存在本身，就是「这条路的结构化输出只是极可能，不是协议保证」的证据。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_prose(content))

    result = await _llm(handler).structured(system="s", user="u", schema=TestOutcome)
    assert result.passed is True
    assert result.output == "ok"


async def test_recovered_json_still_has_to_pass_schema_validation():
    """兜底不等于放行 —— 捞上来的东西照样过 pydantic。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_prose('{"nonsense": 1}'))

    with pytest.raises(LLMError, match="validation failed"):
        await _llm(handler).structured(system="s", user="u", schema=TestOutcome)


async def test_arguments_that_do_not_match_the_schema_raise_llm_error():
    """strict 是 beta 功能，不能假设它永远成立 —— 客户端仍然要校验。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body({"wrong_field": 1}))

    with pytest.raises(LLMError, match="validation failed"):
        await _llm(handler).structured(system="s", user="u", schema=TestOutcome)


async def test_http_error_does_not_leak_the_request_body():
    """请求体里有 Issue 正文。报错信息可以带响应体，但不该回显我们发出去的东西。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    with pytest.raises(LLMError) as exc:
        await _llm(handler).structured(system="SECRET-SYS", user="SECRET-USR", schema=TestOutcome)
    assert "429" in str(exc.value)
    assert "SECRET" not in str(exc.value)


# ------------------------------------------------------------- 用量映射
def test_cache_hit_tokens_map_to_the_cache_read_bucket():
    """OpenAI 语义 → 我们这套（Anthropic 语义）的四个桶。"""
    usage = _usage_of(
        {
            "usage": {
                "prompt_tokens": 5_000,
                "prompt_cache_hit_tokens": 4_000,
                "prompt_cache_miss_tokens": 1_000,
                "completion_tokens": 300,
            }
        }
    )
    assert usage.input_tokens == 1_000  # 只装未命中那部分
    assert usage.cache_read_input_tokens == 4_000
    assert usage.cache_creation_input_tokens == 0  # 自动缓存，不收写入费
    assert usage.output_tokens == 300
    assert usage.total_input_tokens == 5_000  # == prompt_tokens
    assert usage.calls == 1


def test_prompt_tokens_is_not_mapped_straight_onto_input_tokens():
    """★如果直接把 `prompt_tokens` 当 `input_tokens`，命中的部分会被计两遍。"""
    usage = _usage_of(
        {
            "usage": {
                "prompt_tokens": 5_000,
                "prompt_cache_hit_tokens": 4_000,
                "prompt_cache_miss_tokens": 1_000,
                "completion_tokens": 0,
            }
        }
    )
    assert usage.total_input_tokens == 5_000, "重复计数会得到 9000"


def test_missing_miss_field_falls_back_to_subtraction():
    usage = _usage_of(
        {"usage": {"prompt_tokens": 900, "prompt_cache_hit_tokens": 200, "completion_tokens": 10}}
    )
    assert usage.input_tokens == 700


def test_usage_absent_entirely_still_counts_the_call():
    """记账不该比业务还脆：少一个字段不能把整个 run 带崩。"""
    usage = _usage_of({"choices": []})
    assert usage.calls == 1
    assert usage.total_tokens == 0


async def test_usage_accumulates_across_calls_on_one_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_body(
                {"passed": True, "output": "ok"},
                usage={
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                    "completion_tokens": 10,
                },
            ),
        )

    llm = _llm(handler)
    for _ in range(3):
        await llm.structured(system="s", user="u", schema=TestOutcome)

    assert llm.usage.calls == 3
    assert llm.usage.input_tokens == 300
    assert llm.usage.output_tokens == 30


# ------------------------------------------------------------------ 定价
def test_deepseek_prices_are_in_the_default_table():
    """查不到定价的模型成本会是 `None` —— 跑完一轮才发现没有成本数据太晚了。"""
    settings = Settings()
    for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
        assert settings.pricing_for(model) is not None


def test_published_cache_hit_price_is_reproduced_by_the_multiplier():
    """倍率是从官方公布的命中价反推的，这里把它算回去核对一遍。"""
    pricing = Settings().pricing_for("deepseek-v4-pro")
    hit_price = pricing.input_per_mtok * pricing.cache_read_multiplier
    assert hit_price == pytest.approx(0.022, abs=0.0005)


def test_a_run_on_deepseek_costs_a_fraction_of_the_same_run_on_sonnet():
    """项目里唯一的量化卖点是成本，这条把量级钉住，防止定价表被改坏。"""
    from repopilot.llm.usage import Usage, estimate_cost

    settings = Settings()
    usage = Usage(input_tokens=20_000, output_tokens=2_000, calls=3)

    pro = estimate_cost(usage, "deepseek-v4-pro", settings.pricing_for("deepseek-v4-pro"))
    sonnet = estimate_cost(usage, "claude-sonnet-4-6", settings.pricing_for("claude-sonnet-4-6"))

    assert pro.total_usd < sonnet.total_usd / 5


# ------------------------------------------------------------ provider 选择
def test_model_defaults_follow_the_provider(monkeypatch):
    """★换 provider 忘了换 model，会把 claude-sonnet-4-6 发给 DeepSeek。"""
    monkeypatch.setenv("REPOPILOT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("REPOPILOT_DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("REPOPILOT_MODEL", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().model == DEFAULT_MODELS["deepseek"]
    finally:
        get_settings.cache_clear()


def test_an_explicit_model_is_never_overridden(monkeypatch):
    """`model_fields_set` 区分"用户就是要这个"和"用户压根没管"。"""
    monkeypatch.setenv("REPOPILOT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("REPOPILOT_DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("REPOPILOT_MODEL", "deepseek-v4-flash")
    get_settings.cache_clear()
    try:
        assert get_settings().model == "deepseek-v4-flash"
    finally:
        get_settings.cache_clear()


def test_deepseek_without_a_key_degrades_to_scripted(monkeypatch):
    """跑测试和 demo 的人不该被迫先去申请 key。"""
    monkeypatch.setenv("REPOPILOT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("REPOPILOT_DEEPSEEK_API_KEY", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().llm_provider == "scripted"
    finally:
        get_settings.cache_clear()


def test_the_bare_env_var_is_picked_up_too(monkeypatch):
    """官方文档教你 export DEEPSEEK_API_KEY，不会教你加 REPOPILOT_ 前缀。"""
    monkeypatch.setenv("REPOPILOT_LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("REPOPILOT_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-bare")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.llm_provider == "deepseek"
        assert settings.deepseek_api_key == "sk-bare"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("name", "field"),
    [("DEEPSEEK_API_KEY", "deepseek_api_key"), ("ANTHROPIC_API_KEY", "anthropic_api_key")],
)
def test_a_bare_key_in_a_dotenv_file_is_read(tmp_path, monkeypatch, name, field):
    """★这条钉的是一个真踩到的 bug。

    `env_prefix="REPOPILOT_"` 只作用于**字段名推导出来的**变量名。所以
    `.env` 里写裸的 `DEEPSEEK_API_KEY=...` 会被**静默忽略** —— 不报错、
    不警告，只是降级成 scripted，然后你对着一份全 0 的报表发呆。
    而裸名字正是官方文档教的写法，`.env.example` 里也一直是裸的。

    原来的 `os.environ.get()` 兜底只捞得到**进程环境变量**，捞不到 `.env` 文件，
    两条来源只补了一条。修法是字段上挂 `AliasChoices`，一次覆盖两条。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{name}=sk-from-dotenv\n", encoding="utf-8")
    # 进程环境里没有它，只能从 .env 文件读到
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(f"REPOPILOT_{name}", raising=False)

    assert getattr(Settings(), field) == "sk-from-dotenv"
