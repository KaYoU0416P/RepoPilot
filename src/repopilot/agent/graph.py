"""Graph wiring.

    START -> analyze -> plan -> execute -> run_tests -> evaluate -+-> finish -> END
                                   ^                              |
                                   +---------- retry -------------+

Why a graph and not a while loop: the edges are data. The retry path, the budget
check and the exit condition are declared once and are visible/streamable, instead
of being tangled control flow inside one long function.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from repopilot.agent.nodes import Nodes
from repopilot.agent.state import AgentState
from repopilot.llm.base import LLMClient
from repopilot.observability import set_attrs, span
from repopilot.tools import ToolRegistry
from repopilot.workspace import Workspace

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def route_after_evaluate(state: AgentState) -> str:
    """Conditional edge: a pure function of state -> next node name."""
    return "execute" if state.get("verdict") == "retry" else "finish"


def traced(name: str, fn: NodeFn) -> NodeFn:
    """给一个节点套上 span。

    埋点放在**装配处**，而不是 `nodes.py` 里那六个方法上 —— 和
    `ToolRegistry.call` 一处包住六个工具是同一个思路：横切关注点集中在
    "东西被接起来"的那一行，节点方法保持"拿 state、干活、返回 partial"的纯粹。
    以后加第七个节点，它自动就有 span。

    **Java 对照**：这就是 AOP —— 环绕通知织在配置层，业务方法不知道自己被监控着。

    `attempt` 要写进属性：`execute` 在一条 trace 里会出现好几次，
    没有它就分不清眼前这个 span 是第几次重试。
    """

    @functools.wraps(fn)
    async def run_node(state: AgentState) -> dict[str, Any]:
        with span(f"node.{name}", node=name, attempt=state.get("retry_count", 0) + 1) as current:
            partial = await fn(state)
            set_attrs(
                current,
                verdict=partial.get("verdict"),
                files_changed=len(partial.get("files_changed") or []) or None,
            )
            return partial

    return run_node


def build_graph(
    llm: LLMClient, registry: ToolRegistry, workspace: Workspace
) -> CompiledStateGraph:
    nodes = Nodes(llm=llm, registry=registry, workspace=workspace)

    graph = StateGraph(AgentState)
    graph.add_node("analyze", traced("analyze", nodes.analyze))
    graph.add_node("plan", traced("plan", nodes.plan))
    graph.add_node("execute", traced("execute", nodes.execute))
    graph.add_node("run_tests", traced("run_tests", nodes.run_tests))
    graph.add_node("evaluate", traced("evaluate", nodes.evaluate))
    graph.add_node("finish", traced("finish", nodes.finish))

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "run_tests")
    graph.add_edge("run_tests", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluate,
        {"execute": "execute", "finish": "finish"},
    )
    graph.add_edge("finish", END)

    return graph.compile()
