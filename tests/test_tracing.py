"""OTel 埋点的验收测试。

用 `InMemorySpanExporter` 而不是解析控制台输出：span 在内存里是**结构化对象**，
可以直接断言父子关系和属性。这就是 OTel 把 exporter 做成可替换接口的好处 ——
测试和生产走的是同一条 SDK 路径，只换最后一段出口。

**Java 对照**：Spring Boot 测试里的 `InMemorySpanExporter` / `TestSpanHandler`，
一模一样的套路。
"""

import asyncio

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from repopilot.agent.graph import traced
from repopilot.observability import run_id_var, setup_tracing, span
from repopilot.tools import ToolRegistry, ToolResult
from repopilot.tools.base import ToolSpec

_exporter = InMemorySpanExporter()
#: 全局 TracerProvider 一个进程只能装一次，所以在 import 时装好，
#: 用 fixture 在每个用例前清空已收集的 span。
setup_tracing(enabled=True, exporter=_exporter)


@pytest.fixture
def spans():
    _exporter.clear()
    return _exporter


def by_name(exporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


# --------------------------------------------------------------- 基本契约
def test_span_carries_the_current_run_id():
    """run_id 是把 trace 和日志/数据库对起来的那把钥匙。"""
    _exporter.clear()
    token = run_id_var.set("abc12345")
    try:
        with span("thing"):
            pass
    finally:
        run_id_var.reset(token)

    (recorded,) = _exporter.get_finished_spans()
    assert recorded.attributes["run_id"] == "abc12345"


def test_none_attributes_are_dropped_not_stringified(spans):
    """`None` 不该变成字符串 'None' —— 那会污染按属性过滤的查询。"""
    with span("thing", present="yes", missing=None):
        pass
    (recorded,) = spans.get_finished_spans()
    assert recorded.attributes["present"] == "yes"
    assert "missing" not in recorded.attributes


def test_unsupported_attribute_types_are_coerced_instead_of_dropped(spans):
    """OTel 遇到不认识的类型只会打个警告然后**默默丢掉**。这里统一转成 str。"""
    with span("thing", path=__import__("pathlib").Path("/tmp/x")):
        pass
    (recorded,) = spans.get_finished_spans()
    assert recorded.attributes["path"] == "/tmp/x"


def test_an_exception_marks_the_span_red(spans):
    with pytest.raises(ValueError), span("boom"):
        raise ValueError("nope")
    (recorded,) = spans.get_finished_spans()
    assert recorded.status.status_code is StatusCode.ERROR


# ------------------------------------------------------------------ 工具
async def _ok_tool(workspace, **kwargs) -> ToolResult:
    return ToolResult(tool="ok_tool", ok=True, content="fine")


async def _bad_tool(workspace, **kwargs) -> ToolResult:
    raise RuntimeError("tool exploded")


def _registry() -> ToolRegistry:
    reg = ToolRegistry(timeout=5.0, max_concurrency=2)
    reg.register(ToolSpec(name="ok_tool", description="", risk="read", fn=_ok_tool))
    reg.register(ToolSpec(name="bad_tool", description="", risk="write", fn=_bad_tool))
    return reg


async def test_every_tool_call_gets_one_span(spans, workspace):
    await _registry().call("ok_tool", workspace)
    (recorded,) = by_name(spans, "tool.ok_tool")
    assert recorded.attributes["tool.name"] == "ok_tool"
    assert recorded.attributes["tool.risk"] == "read"
    assert recorded.attributes["tool.ok"] is True
    assert recorded.status.status_code is not StatusCode.ERROR


async def test_a_swallowed_tool_error_still_turns_the_span_red(spans, workspace):
    """★这一条是整个文件里最值钱的断言。

    `ToolRegistry.call` 把异常吃成了 `ok=False` 的返回值（故意的：一个坏工具
    不能杀掉整个 run）。于是没有异常冒到 OTel 面前 —— 不手动标错的话，
    一条全是失败的链路在 trace 里显示成全绿，可观测性直接变成误导。
    """
    result = await _registry().call("bad_tool", workspace)
    assert result.ok is False  # 异常确实被吞了

    (recorded,) = by_name(spans, "tool.bad_tool")
    assert recorded.status.status_code is StatusCode.ERROR
    assert "exploded" in recorded.status.description


async def test_unknown_tool_opens_no_span(spans, workspace):
    """没这个工具就没有"调用"发生过，不该凭空造一个 span 出来。"""
    result = await _registry().call("nope", workspace)
    assert result.ok is False
    assert spans.get_finished_spans() == ()


async def test_waiting_for_the_semaphore_counts_as_latency(spans, workspace):
    """并发被信号量卡住的时间也在 span 里 —— 只量执行段会看到假的耗时。"""
    reg = ToolRegistry(timeout=5.0, max_concurrency=1)

    async def slow(workspace, **kwargs) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(tool="slow", ok=True)

    reg.register(ToolSpec(name="slow", description="", risk="read", fn=slow))
    await asyncio.gather(*(reg.call("slow", workspace) for _ in range(3)))

    queued = sorted(s.attributes["tool.queued_ms"] for s in by_name(spans, "tool.slow"))
    assert queued[0] < queued[-1], "第三个调用必须等前两个让出许可"


# ------------------------------------------------------------------ 节点
async def test_node_span_wraps_the_node_and_records_the_verdict(spans):
    async def fake_node(state):
        return {"verdict": "success", "files_changed": ["a.py", "b.py"]}

    await traced("evaluate", fake_node)({"retry_count": 1})

    (recorded,) = by_name(spans, "node.evaluate")
    assert recorded.attributes["node"] == "evaluate"
    assert recorded.attributes["attempt"] == 2  # retry_count 是 0-based
    assert recorded.attributes["verdict"] == "success"
    assert recorded.attributes["files_changed"] == 2


async def test_tool_spans_nest_under_the_node_that_called_them(spans, workspace):
    """父子关系靠 ContextVar 隐式传播，包括 `asyncio.gather` 分出去的协程。

    这正是 `plan` 节点那一把并发工具调用能整整齐齐挂在一个节点 span 底下的原因：
    创建 Task 时会**拷贝**一份当前上下文。Java 那边靠 ThreadLocal + 手动跨线程
    传播（`ContextSnapshot`），Python 这里是语言自带的。
    """
    reg = _registry()

    async def node(state):
        await asyncio.gather(*(reg.call("ok_tool", workspace) for _ in range(3)))
        return {}

    await traced("plan", node)({"retry_count": 0})

    (parent,) = by_name(spans, "node.plan")
    children = by_name(spans, "tool.ok_tool")
    assert len(children) == 3
    assert {c.parent.span_id for c in children} == {parent.context.span_id}
    # 同一条 trace 才是"一次 run 一条链路"，不是三条孤儿
    assert {c.context.trace_id for c in children} == {parent.context.trace_id}


async def test_node_names_survive_the_wrapper():
    """LangGraph 会拿函数签名去判断怎么调用节点，`functools.wraps` 不能省。"""

    async def analyze(state):
        return {}

    assert traced("analyze", analyze).__name__ == "analyze"


# --------------------------------------------------------------- LLM 调用
class _Block:
    type = "tool_use"
    input = {"passed": True, "output": "ok"}


class _Response:
    stop_reason = "tool_use"
    content = [_Block()]

    class usage:  # noqa: N801 - 模仿 SDK 的属性形状
        input_tokens = 1_200
        output_tokens = 300
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 2_048


class _Messages:
    async def create(self, **kwargs):
        return _Response()


async def test_the_llm_span_carries_the_tokens_that_the_money_is_made_of(spans):
    """节点 span 的耗时九成在这一段，token 属性让"慢"和"贵"在同一条链路上对得上号。"""
    from repopilot.agent.schemas import TestOutcome
    from repopilot.llm.anthropic_client import AnthropicLLM

    llm = AnthropicLLM(api_key="not-used", model="claude-sonnet-4-6")
    llm._client = type("C", (), {"messages": _Messages()})()

    await llm.structured(system="s", user="u", schema=TestOutcome)

    (recorded,) = by_name(spans, "llm.structured")
    assert recorded.attributes["llm.model"] == "claude-sonnet-4-6"
    assert recorded.attributes["llm.schema"] == "TestOutcome"
    # 1200 未命中 + 2048 读缓存 —— 只报 input_tokens 会少算 2048
    assert recorded.attributes["llm.input_tokens"] == 3_248
    assert recorded.attributes["llm.output_tokens"] == 300
    assert recorded.attributes["llm.cache_read_tokens"] == 2_048


# ------------------------------------------------------------ 关掉的时候
def test_instrumentation_is_free_when_tracing_is_off():
    """没装配 provider 时 `trace.get_tracer()` 返回 no-op 实现 ——
    所以埋点代码可以无条件写，不需要在每处 `if enabled:`。
    （这里只能验证不抛异常：本进程已经装配过了，装不回去。）
    """
    from opentelemetry import trace

    noop = trace.NoOpTracer()
    with noop.start_as_current_span("free") as s:
        s.set_attribute("k", "v")
    assert s.is_recording() is False
