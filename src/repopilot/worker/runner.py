"""执行**一个** run：建 workspace → 跑 graph → 落库。

和队列解耦：runner 不知道任务从哪来，只知道怎么把一个 RunRow 跑完。
"""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

from repopilot.agent import build_graph, initial_state
from repopilot.api.schemas import RunEvent
from repopilot.config import Settings
from repopilot.db import runs as runs_repo
from repopilot.db.models import RunRow
from repopilot.domain import RunStatus
from repopilot.evaluation import evaluate_run
from repopilot.llm import build_llm
from repopilot.observability import get_logger, run_id_var, set_attrs, span
from repopilot.tools import build_registry
from repopilot.worker.bus import EventBus
from repopilot.workspace import WorkspaceManager

log = get_logger(__name__)


class RunFailed(RuntimeError):
    """Agent 跑完但没成功。和「进程炸了」区分开，前者不该重试。"""


class Runner:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings = settings
        self.bus = bus
        self.workspaces = WorkspaceManager(settings.workspace_root)

    async def execute(self, row: RunRow, worker_id: str) -> None:
        """跑完一个 run，并把终态写回数据库。"""
        token = run_id_var.set(str(row.id)[:8])
        seq = 0
        started = time.perf_counter()

        def emit(event_type: str, node: str | None = None, message: str = "", **data) -> None:
            nonlocal seq
            seq += 1
            self.bus.publish(
                row.id,
                RunEvent(
                    run_id=str(row.id),
                    seq=seq,
                    type=event_type,  # type: ignore[arg-type]
                    node=node,
                    message=message,
                    data=data,
                ),
            )

        heartbeat_task: asyncio.Task | None = None
        try:
            workspace = self.workspaces.create(Path(row.repo_path), run_id=str(row.id))
            emit("run_started", message=f"workspace 就绪: {workspace.root.name}")

            # 续租心跳：Agent 可能跑好几分钟，租约不能中途过期
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(row.id, worker_id), name=f"hb-{row.id}"
            )

            graph = build_graph(build_llm(), build_registry(), workspace)
            state = initial_state(
                str(row.id), row.task, row.repo_path, self.settings.max_retries
            )

            # 一次 run = 一条 trace 的根 span。六个节点 span、几十个工具 span
            # 都靠 ContextVar 自动挂在它底下 —— LangGraph 给节点开新 Task 时会
            # **拷贝**一份当前上下文，所以父子关系不用我们手工往下传。
            #
            # 已知取舍：根 span 只圈住"跑图 + 评估"，不含建 workspace 和落库。
            # 那两段目前没埋点，圈进来也只是一段空白；真要做端到端延迟归因时再扩。
            with span("run", run_id_full=str(row.id), attempt=row.attempts) as run_span:
                async for chunk in graph.astream(state, stream_mode="updates"):
                    for node_name, partial in chunk.items():
                        state = {**state, **_merge(state, partial)}
                        emit(
                            "node_completed",
                            node=node_name,
                            message=(partial.get("step_log") or [node_name])[-1],
                            verdict=state.get("verdict"),
                            retry_count=state.get("retry_count", 0),
                        )

                report = evaluate_run(state)
                usage = report.usage
                set_attrs(
                    run_span,
                    verdict=state.get("verdict"),
                    task_success=report.task_success,
                    retries=state.get("retry_count", 0),
                    **{
                        "llm.calls": usage.usage.calls,
                        "llm.total_tokens": usage.usage.total_tokens,
                        "llm.cost_usd": usage.cost_usd,
                    },
                )
            duration_ms = int((time.perf_counter() - started) * 1000)

            if report.task_success:
                # 成功不等于结束 —— 产物要等人批准才能开 PR
                await runs_repo.transition(
                    row.id,
                    RunStatus.PENDING_APPROVAL,
                    verdict=state.get("verdict"),
                    diff=state.get("diff"),
                    final_report=state.get("final_report"),
                    evaluation=report.model_dump(),
                    step_log=state.get("step_log") or [],
                    files_changed=state.get("files_changed") or [],
                    retry_count=state.get("retry_count", 0),
                    # 我们这段活干完了，租约交还。不交的话 publisher 要等它
                    # 自然过期才能接手，人一批准就卡住两分钟。
                    **runs_repo.RELEASE_LEASE,
                )
                emit(
                    "run_finished",
                    message="Agent 完成，等待人工审批",
                    verdict=state.get("verdict"),
                    evaluation=report.model_dump(),
                    duration_ms=duration_ms,
                )
            else:
                await runs_repo.transition(
                    row.id,
                    RunStatus.FAILED,
                    verdict=state.get("verdict"),
                    diff=state.get("diff"),
                    final_report=state.get("final_report"),
                    evaluation=report.model_dump(),
                    step_log=state.get("step_log") or [],
                    files_changed=state.get("files_changed") or [],
                    retry_count=state.get("retry_count", 0),
                    error=f"agent 未能完成任务: {report.failure_reason}",
                    finished_at=datetime.now(UTC),
                    **runs_repo.RELEASE_LEASE,
                )
                emit("run_error", message=f"失败: {report.failure_reason}")

        except asyncio.CancelledError:
            # 不改状态：租约会过期，任务自然回到队列被别人接手
            emit("run_error", message="worker 被取消，任务将由租约回收")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("run 执行异常")
            await self._fail(row, f"{type(exc).__name__}: {exc}")
            emit("run_error", message=str(exc))
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            self.bus.close(row.id)
            run_id_var.reset(token)

    async def _heartbeat_loop(self, run_id, worker_id: str) -> None:
        interval = self.settings.lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            alive = await runs_repo.heartbeat(run_id, worker_id, self.settings.lease_seconds)
            if not alive:
                log.warning("run=%s 租约已丢失，停止续租", run_id)
                return

    async def _fail(self, row: RunRow, error: str) -> None:
        """异常路径：还有重试次数就退回队列，否则标记失败。"""
        try:
            if row.attempts < row.max_attempts:
                await runs_repo.transition(row.id, RunStatus.QUEUED, error=error)
            else:
                await runs_repo.transition(row.id, RunStatus.FAILED, error=error)
        except Exception:  # noqa: BLE001
            log.exception("写入失败状态时又失败了 run=%s", row.id)


def _merge(state: dict, partial: dict) -> dict:
    """镜像 LangGraph 的 reducer，让我们手上这份 state 副本保持准确。"""
    merged = {}
    for key, value in partial.items():
        if key in ("tool_calls", "errors", "step_log"):
            merged[key] = (state.get(key) or []) + list(value or [])
        else:
            merged[key] = value
    return merged
