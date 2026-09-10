"""Agent behaviour tests: the graph, not just individual functions."""

import pytest

from repopilot.agent import build_graph, initial_state, route_after_evaluate
from repopilot.agent.schemas import TestOutcome
from repopilot.evaluation import evaluate_run
from repopilot.llm import ScriptedLLM

TASK = "Fix divide() in calculator.py so dividing by zero raises ValueError."


def test_conditional_edge_routes_on_verdict():
    assert route_after_evaluate({"verdict": "retry"}) == "execute"
    assert route_after_evaluate({"verdict": "success"}) == "finish"
    assert route_after_evaluate({"verdict": "failed"}) == "finish"


async def test_evaluate_stops_retrying_at_budget(workspace, registry):
    from repopilot.agent.nodes import Nodes

    nodes = Nodes(ScriptedLLM(), registry, workspace)
    state = initial_state("t", TASK, str(workspace.root), max_retries=1)
    state["test_result"] = TestOutcome(passed=False, output="boom")

    first = await nodes.evaluate(state)
    assert first["verdict"] == "retry"
    assert first["retry_count"] == 1

    exhausted = await nodes.evaluate({**state, "retry_count": 1})
    assert exhausted["verdict"] == "failed"


@pytest.mark.slow
async def test_full_loop_fixes_the_bug(workspace, registry):
    """End-to-end: analyze -> plan -> execute -> run_tests -> evaluate -> finish."""
    graph = build_graph(ScriptedLLM(), registry, workspace)
    state = await graph.ainvoke(
        initial_state(workspace.run_id, TASK, str(workspace.root), max_retries=1)
    )

    assert state["verdict"] == "success"
    assert state["files_changed"] == ["calculator.py"]
    assert state["test_result"].passed
    assert "raise ValueError" in state["diff"]

    visited = [line.split(":")[0] for line in state["step_log"]]
    assert visited[:4] == ["analyze", "plan", "execute attempt 1", "run_tests"]

    report = evaluate_run(state)
    assert report.task_success
    assert report.retry_count == 0
    assert report.diff_valid
    assert report.failure_reason == "none"
    assert report.tool_selection["write_file"] == 1

    # 计量走通了：三个节点各调一次 LLM，用量从客户端流到 state 再到 RunEvaluation。
    # ScriptedLLM 不花钱，所以 token 是 0、成本是「未知」（它不在定价表里）——
    # 这条同时钉住了"测试不烧 token"。
    assert report.usage.usage.calls == 3
    assert report.usage.usage.total_tokens == 0
    assert report.usage.cost_usd is None
    assert "llm calls: 3" in state["final_report"]
