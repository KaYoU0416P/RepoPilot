"""The graph's shared state.

LangGraph state is a TypedDict, not a class. A node returns a *partial* dict and
LangGraph merges it in. Keys annotated with a reducer (e.g. operator.add) are
combined instead of overwritten - that is how we accumulate logs across retries
without a node having to read-modify-write the whole list.
"""

import operator
from typing import Annotated, Literal, TypedDict

from repopilot.agent.schemas import Analysis, EditSet, Plan, TestOutcome, ToolCallRecord

Verdict = Literal["success", "retry", "failed"]


class AgentState(TypedDict, total=False):
    # --- input ---
    run_id: str
    task: str
    repo_path: str

    # --- produced by nodes ---
    analysis: Analysis | None
    plan: Plan | None
    edits: EditSet | None
    files_changed: list[str]
    test_result: TestOutcome | None
    diff: str
    verdict: Verdict
    final_report: str

    # --- accumulated (reducers merge instead of replace) ---
    tool_calls: Annotated[list[ToolCallRecord], operator.add]
    errors: Annotated[list[str], operator.add]
    step_log: Annotated[list[str], operator.add]

    # --- budget ---
    retry_count: int
    max_retries: int


def initial_state(run_id: str, task: str, repo_path: str, max_retries: int) -> AgentState:
    return AgentState(
        run_id=run_id,
        task=task,
        repo_path=repo_path,
        analysis=None,
        plan=None,
        edits=None,
        files_changed=[],
        test_result=None,
        diff="",
        verdict="retry",
        final_report="",
        tool_calls=[],
        errors=[],
        step_log=[],
        retry_count=0,
        max_retries=max_retries,
    )
