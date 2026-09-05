"""Run-level evaluation.

The point: we do not judge the agent only by its final answer. We judge the
trajectory - which tools it chose, how many attempts it burned, whether the diff
is real, and why it failed.
"""

from typing import Literal

from pydantic import BaseModel

from repopilot.agent.state import AgentState

FailureReason = Literal[
    "none",
    "no_edits_produced",
    "tests_failed",
    "tests_timed_out",
    "tool_errors",
    "budget_exhausted",
]


class RunEvaluation(BaseModel):
    task_success: bool
    tests_passed: bool
    retry_count: int
    tool_calls_total: int
    tool_calls_failed: int
    tool_selection: dict[str, int]
    diff_valid: bool
    files_changed: int
    failure_reason: FailureReason


def evaluate_run(state: AgentState) -> RunEvaluation:
    calls = state.get("tool_calls") or []
    test = state.get("test_result")
    diff = state.get("diff") or ""
    changed = state.get("files_changed") or []

    selection: dict[str, int] = {}
    for call in calls:
        selection[call.tool] = selection.get(call.tool, 0) + 1

    diff_valid = bool(diff.strip()) and diff.strip() != "(no changes)"
    tests_passed = bool(test and test.passed)
    success = state.get("verdict") == "success" and tests_passed and diff_valid

    return RunEvaluation(
        task_success=success,
        tests_passed=tests_passed,
        retry_count=state.get("retry_count", 0),
        tool_calls_total=len(calls),
        tool_calls_failed=sum(1 for c in calls if not c.ok),
        tool_selection=selection,
        diff_valid=diff_valid,
        files_changed=len(changed),
        failure_reason=_failure_reason(state, tests_passed, diff_valid),
    )


def _failure_reason(state: AgentState, tests_passed: bool, diff_valid: bool) -> FailureReason:
    if state.get("verdict") == "success" and tests_passed and diff_valid:
        return "none"
    test = state.get("test_result")
    if test and test.timed_out:
        return "tests_timed_out"
    if not diff_valid:
        return "no_edits_produced"
    if not tests_passed and state.get("retry_count", 0) >= state.get("max_retries", 0):
        return "budget_exhausted"
    if not tests_passed:
        return "tests_failed"
    if state.get("errors"):
        return "tool_errors"
    return "budget_exhausted"
