"""The six graph nodes.

A node's contract: take the current AgentState, do one job, return a PARTIAL state
dict. Never mutate the state you were given - LangGraph merges what you return.
"""

import asyncio
import json
from typing import Any

from repopilot.agent import prompts
from repopilot.agent.schemas import (
    Analysis,
    EditSet,
    Plan,
    TestOutcome,
    ToolCallRecord,
)
from repopilot.agent.state import AgentState
from repopilot.llm.base import LLMClient, LLMError
from repopilot.llm.usage import UsageReport, report_for
from repopilot.observability import get_logger
from repopilot.tools import ToolRegistry, ToolResult
from repopilot.workspace import Workspace

log = get_logger(__name__)


def _record(result: ToolResult, **args: Any) -> ToolCallRecord:
    preview = json.dumps(args, default=str)[:120]
    return ToolCallRecord(
        tool=result.tool,
        ok=result.ok,
        duration_ms=result.duration_ms,
        args_preview=preview,
        error=result.error,
    )


class Nodes:
    """Holds the dependencies so each node method is a plain (state) -> partial state."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, workspace: Workspace) -> None:
        self.llm = llm
        self.registry = registry
        self.ws = workspace

    # ------------------------------------------------------------------ analyze
    async def analyze(self, state: AgentState) -> dict[str, Any]:
        """Look at the repo tree, ask the LLM what is relevant."""
        listing = await self.registry.call("list_files", self.ws, glob="**/*.py")
        analysis = await self.llm.structured(
            system=prompts.SYSTEM,
            user=prompts.ANALYZE_USER.format(
                task=prompts.fence_task(state["task"]), tree=listing.content
            ),
            schema=Analysis,
        )
        return {
            "analysis": analysis,
            "tool_calls": [_record(listing, glob="**/*.py")],
            "step_log": [f"analyze: {len(analysis.relevant_files)} candidate files"],
        }

    # --------------------------------------------------------------------- plan
    async def plan(self, state: AgentState) -> dict[str, Any]:
        """Gather evidence (concurrently), then commit to one strategy."""
        analysis = state["analysis"]
        assert analysis is not None

        # asyncio.gather runs the coroutines concurrently on one event loop thread.
        # The registry's Semaphore still caps how many actually run at a time.
        read_jobs = [
            self.registry.call("read_file", self.ws, path=p) for p in analysis.relevant_files[:5]
        ]
        search_jobs = [
            self.registry.call("search_code", self.ws, pattern=q)
            for q in analysis.search_queries[:3]
        ]
        results = await asyncio.gather(*read_jobs, *search_jobs)

        evidence = "\n\n".join(
            f"### {r.meta.get('path', r.tool)}\n{r.content}" for r in results if r.ok
        )
        records = [_record(r) for r in results]

        plan = await self.llm.structured(
            system=prompts.SYSTEM,
            user=prompts.PLAN_USER.format(
                task=prompts.fence_task(state["task"]),
                reasoning=analysis.reasoning,
                evidence=evidence or "(no evidence gathered)",
            ),
            schema=Plan,
        )
        return {
            "plan": plan,
            "tool_calls": records,
            "step_log": [f"plan: {plan.summary}"],
        }

    # ------------------------------------------------------------------ execute
    async def execute(self, state: AgentState) -> dict[str, Any]:
        """Read current contents, ask for full-file rewrites, write them.

        On a retry the failing test output is appended to the prompt, which is the
        only thing that makes attempt N+1 different from attempt N.
        """
        plan = state["plan"]
        assert plan is not None
        attempt = state["retry_count"] + 1

        analysis = state.get("analysis")
        targets = plan.files_to_edit[:5] or (analysis.relevant_files[:2] if analysis else [])
        reads = await asyncio.gather(
            *(self.registry.call("read_file", self.ws, path=p) for p in targets)
        )
        current = "\n\n".join(
            f"### {r.meta['path']}\n```\n{_strip_line_numbers(r.content)}\n```"
            for r in reads
            if r.ok
        )

        feedback = ""
        last = state.get("test_result")
        if last is not None and not last.passed:
            feedback = prompts.RETRY_FEEDBACK.format(
                attempt=attempt,
                max_attempts=state["max_retries"] + 1,
                test_output=last.output[-3000:],
            )

        try:
            edit_set = await self.llm.structured(
                system=prompts.SYSTEM,
                user=prompts.EXECUTE_USER.format(
                    task=prompts.fence_task(state["task"]),
                    summary=plan.summary,
                    approach=plan.approach,
                    files=current or "(no files read)",
                    feedback=feedback,
                ),
                schema=EditSet,
            )
        except LLMError as exc:
            return {
                "errors": [f"execute: {exc}"],
                "tool_calls": [_record(r) for r in reads],
                "step_log": ["execute: LLM failed to produce edits"],
            }

        writes = []
        for edit in edit_set.edits[:5]:
            writes.append(
                await self.registry.call(
                    "write_file", self.ws, path=edit.path, content=edit.content
                )
            )

        changed = [e.path for e, w in zip(edit_set.edits, writes, strict=False) if w.ok]
        return {
            "edits": edit_set,
            "files_changed": changed,
            "tool_calls": [_record(r) for r in reads] + [_record(w) for w in writes],
            "errors": [w.error for w in writes if w.error],
            "step_log": [f"execute attempt {attempt}: wrote {len(changed)} file(s)"],
        }

    # ---------------------------------------------------------------- run_tests
    async def run_tests(self, state: AgentState) -> dict[str, Any]:
        result = await self.registry.call(
            "run_tests", self.ws, timeout=None
        )
        outcome = TestOutcome(
            passed=bool(result.meta.get("passed")),
            exit_code=result.meta.get("exit_code"),
            timed_out=bool(result.meta.get("timed_out")),
            summary=_last_line(result.content),
            output=result.content,
        )
        return {
            "test_result": outcome,
            "tool_calls": [_record(result)],
            "step_log": [f"run_tests: {'PASS' if outcome.passed else 'FAIL'} ({outcome.summary})"],
        }

    # ----------------------------------------------------------------- evaluate
    async def evaluate(self, state: AgentState) -> dict[str, Any]:
        """Decide the verdict. The conditional edge only *reads* this, never recomputes it."""
        test = state.get("test_result")
        if test is not None and test.passed and state.get("files_changed"):
            return {"verdict": "success", "step_log": ["evaluate: success"]}

        # ★重试之前先问一句「钱还够吗」。
        #
        # 熔断本身在 `BudgetedLLM` 里（那是硬兜底，会抛 `BudgetExceeded`），
        # 但让它在下一次调用时炸出来是很糟的用户体验：报告里只剩一个异常。
        # 在这里主动停，run 就能干干净净地落到 `failed`，报告里写清楚
        # 「是预算掐的，不是改不出来」—— **两种失败的处理方式完全不同**。
        breach = self._budget_breach()
        if breach is not None:
            log.warning("run 被成本熔断掐停：%s", breach)
            return {
                "verdict": "failed",
                "errors": [f"budget: {breach}"],
                "step_log": [f"evaluate: 预算耗尽，停止重试（{breach}）"],
            }

        if state["retry_count"] < state["max_retries"]:
            n = state["retry_count"] + 1
            return {
                "verdict": "retry",
                "retry_count": n,
                "step_log": [f"evaluate: retry {n}/{state['max_retries']}"],
            }

        return {"verdict": "failed", "step_log": ["evaluate: budget exhausted"]}

    # ------------------------------------------------------------------- finish
    async def finish(self, state: AgentState) -> dict[str, Any]:
        diff = await self.registry.call("git_diff", self.ws)
        verdict = state.get("verdict", "failed")
        test = state.get("test_result")
        usage = self._usage_report()

        lines = [
            f"verdict: {verdict}",
            f"files changed: {', '.join(state.get('files_changed') or []) or 'none'}",
            f"tests: {'passed' if test and test.passed else 'failed'}"
            + (f" ({test.summary})" if test else ""),
            f"attempts: {state['retry_count'] + 1}",
            f"tool calls: {len(state.get('tool_calls') or [])}",
            f"llm calls: {usage.usage.calls}  tokens: {usage.usage.total_tokens}"
            + (f"  cost: ${usage.cost_usd:.4f}" if usage.cost_usd is not None else "  cost: ?"),
        ]
        if state.get("errors"):
            lines.append(f"errors: {len(state['errors'])}")

        return {
            "diff": diff.content,
            "final_report": "\n".join(lines),
            "usage": usage,
            "tool_calls": [_record(diff)],
            "step_log": ["finish"],
        }

    def _budget_breach(self) -> str | None:
        """超预算了吗。返回一句人话，或者 `None`。

        `getattr` 兜底是因为**注入进来的不一定是 `BudgetedLLM`** ——
        测试会塞各种假客户端，评测 harness 也允许自定义 agent。
        节点不该因为「预算这个能力恰好没装」就崩掉。
        """
        breach = getattr(self.llm, "breach", None)
        return breach() if callable(breach) else None

    def _usage_report(self) -> UsageReport:
        """把客户端的累计用量折算成成本。

        在 finish 里一次性读走，而不是每个节点各记一笔：客户端本身就是
        per-run 的累加器，节点再做一次加法只会重复计数。

        实现落在 `llm/usage.py::report_for` —— **崩掉的 run 走不到 finish**，
        评测 harness 要在异常路径上收同一笔账。
        """
        return report_for(self.llm)


def _strip_line_numbers(text: str) -> str:
    """read_file returns '  12 | code'. The LLM must rewrite raw content, not that."""
    out = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" | ")
        out.append(tail if sep and head.strip().isdigit() else line)
    return "\n".join(out)


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""
