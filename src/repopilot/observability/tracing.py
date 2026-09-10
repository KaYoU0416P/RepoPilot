"""OpenTelemetry 链路追踪。日志回答"发生了什么"，trace 回答"时间花在哪、谁调了谁"。

这就是 Java 那边 SkyWalking / Zipkin / Micrometer Tracing 的同一组概念：
trace（一次完整请求）→ span（其中一段工作）→ context 传播（父子关系怎么传下去）。
换掉的只有传播的载体：**Java 用 ThreadLocal，Python asyncio 用 ContextVar**。

## 为什么这个文件里没有一处 `if enabled:`

OTel 分成 **api** 和 **sdk** 两个包，这个拆分不是包管理洁癖：
没装配 provider 时，`trace.get_tracer()` 返回的是一个**空实现**（no-op），
`start_as_current_span` 什么都不做、不分配、不记录。

所以埋点代码可以无条件写，开关只在 `setup_tracing()` 一处。
**Java 对照**：SLF4J 的 API 和 logback 实现是两个 jar，没绑定实现时日志静默丢弃 ——
调用方不需要写 `if (logger != null)`。同一个套路。

## 为什么 ConsoleSpanExporter 默认写 stderr

`ConsoleSpanExporter` 官方默认写 **stdout**。在这个项目里那是个会炸的默认值：
`mcp/server.py` 走 stdio 传输，stdout 就是 JSON-RPC 的协议通道，
往里吐一坨 span JSON 等于给对端发畸形消息。`setup_logging` 已经踩过这个坑，
这里把默认值直接改成 stderr，而不是指望每个调用方记得传参 ——
**危险的默认值应该在库这一层就修掉。**
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Status, StatusCode

from repopilot.observability.logging import run_id_var

#: 模块级 tracer。装配之前拿到的是 `ProxyTracer`，它在第一次开 span 时才去
#: 解析真正的 provider —— 所以 import 顺序和 `setup_tracing()` 的先后无关。
_tracer = trace.get_tracer("repopilot")

_configured = False


def setup_tracing(
    *,
    enabled: bool = True,
    service_name: str = "repopilot",
    stream: TextIO | None = None,
    exporter: SpanExporter | None = None,
) -> bool:
    """装配全局 TracerProvider。返回是否真的开启了。

    `enabled=False` 时**什么都不做**，埋点自动退化成 no-op（见模块头注释）。

    用 `SimpleSpanProcessor` 而不是生产环境常见的 `BatchSpanProcessor`：
    batch 会攒一批再后台线程发，进程退出时没 flush 的那批直接丢 ——
    对着控制台调试时，"span 有时候不出现"比慢一点糟糕得多。
    真接后端了再换 batch。
    """
    global _configured
    if not enabled or _configured:
        return _configured

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        SimpleSpanProcessor(exporter or ConsoleSpanExporter(out=stream or sys.stderr))
    )
    trace.set_tracer_provider(provider)
    _configured = True
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """开一个 span，自动挂上当前的 `run_id`。

    父子关系不用手工传：`start_as_current_span` 把新 span 写进 ContextVar，
    嵌套调用（包括 `asyncio.gather` 出去的协程 —— Task 创建时会**拷贝**一份
    上下文）自动认它当父亲。这就是 `plan` 节点那 8 个并发工具调用能整整齐齐
    挂在一个节点 span 底下的原因。

    `run_id` 冗余地写在每个 span 上，而不是只写在根 span：
    trace 后端按属性检索是 per-span 的，只有根节点带 run_id 就没法直接
    "查这个 run 的所有慢工具"。这点冗余换的是可检索性。
    """
    with _tracer.start_as_current_span(name) as current:
        set_attrs(current, run_id=run_id_var.get(), **attributes)
        yield current


def set_attrs(current: Span, **attributes: Any) -> None:
    """写属性，`None` 一律跳过。

    OTel 的属性值只接受 str/bool/int/float（和它们的序列）。塞别的进去不会抛，
    只会打一条警告然后丢掉 —— 又一个"静默失效"。这里统一兜住：
    认识的类型直传，不认识的转成 str。
    """
    for key, value in attributes.items():
        if value is None:
            continue
        known = isinstance(value, str | bool | int | float)
        current.set_attribute(key, value if known else str(value))


def mark_error(current: Span, message: str) -> None:
    """把 span 标红。

    ★这个函数存在的理由，是这个项目里最容易被 trace 骗到的地方：
    `ToolRegistry.call` 和 publisher 都会**把异常吞掉**换成一个 `ok=False` 的
    返回值（故意的：一个坏工具不能杀掉整个 run）。可是 OTel 只在**异常冒出去**
    时才自动标错 —— 于是一条全是失败的链路会显示成绿色。
    **凡是把异常转成返回值的地方，都得手动把这个状态补回去。**
    """
    current.set_status(Status(StatusCode.ERROR, message[:200]))
