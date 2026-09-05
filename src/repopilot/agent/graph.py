"""Graph wiring.

    START -> analyze -> plan -> execute -> run_tests -> evaluate -+-> finish -> END
                                   ^                              |
                                   +---------- retry -------------+

Why a graph and not a while loop: the edges are data. The retry path, the budget
check and the exit condition are declared once and are visible/streamable, instead
of being tangled control flow inside one long function.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from repopilot.agent.nodes import Nodes
from repopilot.agent.state import AgentState
from repopilot.llm.base import LLMClient
from repopilot.tools import ToolRegistry
from repopilot.workspace import Workspace


def route_after_evaluate(state: AgentState) -> str:
    """Conditional edge: a pure function of state -> next node name."""
    return "execute" if state.get("verdict") == "retry" else "finish"


def build_graph(
    llm: LLMClient, registry: ToolRegistry, workspace: Workspace
) -> CompiledStateGraph:
    nodes = Nodes(llm=llm, registry=registry, workspace=workspace)

    graph = StateGraph(AgentState)
    graph.add_node("analyze", nodes.analyze)
    graph.add_node("plan", nodes.plan)
    graph.add_node("execute", nodes.execute)
    graph.add_node("run_tests", nodes.run_tests)
    graph.add_node("evaluate", nodes.evaluate)
    graph.add_node("finish", nodes.finish)

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
