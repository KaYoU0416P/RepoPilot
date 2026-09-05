"""Run orchestration: owns workspaces, background tasks and the event fan-out.

This is the layer between HTTP and the graph. FastAPI handlers stay thin; nothing
here knows about Request/Response objects.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from repopilot.agent import build_graph, initial_state
from repopilot.agent.state import AgentState
from repopilot.api.schemas import RunEvent, RunResponse, RunStatus
from repopilot.config import Settings
from repopilot.evaluation import RunEvaluation, evaluate_run
from repopilot.llm import build_llm
from repopilot.observability import get_logger, run_id_var
from repopilot.tools import build_registry
from repopilot.workspace import Workspace, WorkspaceManager

log = get_logger(__name__)

_SENTINEL = object()


@dataclass
class RunRecord:
    run_id: str
    task: str
    status: RunStatus = "pending"
    started_at: float = field(default_factory=time.perf_counter)
    duration_ms: int | None = None
    state: AgentState | None = None
    evaluation: RunEvaluation | None = None
    error: str | None = None
    events: list[RunEvent] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    task_handle: asyncio.Task | None = None
    workspace: Workspace | None = None

    def publish(self, event: RunEvent) -> None:
        self.events.append(event)
        for queue in self.subscribers:
            queue.put_nowait(event)

    def close_streams(self) -> None:
        for queue in self.subscribers:
            queue.put_nowait(_SENTINEL)


class RunService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspaces = WorkspaceManager(settings.workspace_root)
        self._runs: dict[str, RunRecord] = {}

    # ------------------------------------------------------------------ public
    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def list_runs(self) -> list[RunRecord]:
        return list(self._runs.values())

    async def start(self, task: str, repo_path: Path, max_retries: int) -> RunRecord:
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(run_id=run_id, task=task)
        self._runs[run_id] = record
        # create_task schedules the coroutine on the running loop and returns
        # immediately - this is what makes POST /runs respond in milliseconds.
        record.task_handle = asyncio.create_task(
            self._execute(record, repo_path, max_retries), name=f"run-{run_id}"
        )
        return record

    async def cancel(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if record is None or record.task_handle is None or record.task_handle.done():
            return False
        record.task_handle.cancel()
        return True

    async def subscribe(self, record: RunRecord) -> asyncio.Queue:
        """Replay past events, then receive live ones. Late subscribers see everything."""
        queue: asyncio.Queue = asyncio.Queue()
        for event in record.events:
            queue.put_nowait(event)
        if record.status in ("succeeded", "failed", "error"):
            queue.put_nowait(_SENTINEL)
        else:
            record.subscribers.append(queue)
        return queue

    def unsubscribe(self, record: RunRecord, queue: asyncio.Queue) -> None:
        if queue in record.subscribers:
            record.subscribers.remove(queue)

    def to_response(self, record: RunRecord) -> RunResponse:
        state = record.state or {}
        return RunResponse(
            run_id=record.run_id,
            status=record.status,
            task=record.task,
            verdict=state.get("verdict"),
            final_report=state.get("final_report"),
            diff=state.get("diff"),
            files_changed=state.get("files_changed") or [],
            steps=state.get("step_log") or [],
            evaluation=record.evaluation,
            error=record.error,
            duration_ms=record.duration_ms,
        )

    # ----------------------------------------------------------------- private
    async def _execute(self, record: RunRecord, repo_path: Path, max_retries: int) -> None:
        token = run_id_var.set(record.run_id)
        seq = 0

        def emit(event_type, node=None, message="", **data) -> None:
            nonlocal seq
            seq += 1
            record.publish(
                RunEvent(
                    run_id=record.run_id,
                    seq=seq,
                    type=event_type,
                    node=node,
                    message=message,
                    data=data,
                )
            )

        try:
            record.status = "running"
            workspace = self.workspaces.create(repo_path, run_id=record.run_id)
            record.workspace = workspace
            emit("run_started", message=f"workspace ready: {workspace.root.name}")

            graph = build_graph(build_llm(), build_registry(), workspace)
            state = initial_state(record.run_id, record.task, str(repo_path), max_retries)

            # stream_mode="updates" yields {node_name: partial_state} after each node.
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

            record.state = state
            record.evaluation = evaluate_run(state)
            record.status = "succeeded" if record.evaluation.task_success else "failed"
            emit(
                "run_finished",
                message=state.get("final_report", ""),
                verdict=state.get("verdict"),
                evaluation=record.evaluation.model_dump(),
            )

        except asyncio.CancelledError:
            record.status = "error"
            record.error = "cancelled"
            emit("run_error", message="run cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("run failed")
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            emit("run_error", message=record.error)
        finally:
            record.duration_ms = int((time.perf_counter() - record.started_at) * 1000)
            record.close_streams()
            run_id_var.reset(token)


def _merge(state: AgentState, partial: dict) -> dict:
    """Mirror LangGraph's reducers so our streamed copy of state stays accurate."""
    merged = {}
    for key, value in partial.items():
        if key in ("tool_calls", "errors", "step_log"):
            merged[key] = (state.get(key) or []) + list(value or [])
        else:
            merged[key] = value
    return merged
