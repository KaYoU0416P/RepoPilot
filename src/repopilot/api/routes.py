"""HTTP 接口层。

处理函数保持很薄：校验 → 调仓储/服务 → 组装响应。
业务规则在 domain/ 和 db/ 里，不在这里。
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from repopilot.api.schemas import (
    ApprovalRequest,
    CreateRunRequest,
    CreateRunResponse,
    HealthResponse,
    RunEvent,
    RunResponse,
)
from repopilot.config import Settings, get_settings
from repopilot.db import approvals as approvals_repo
from repopilot.db import runs as runs_repo
from repopilot.db.models import RunRow
from repopilot.domain import ACTIVE, InvalidTransition, RunStatus
from repopilot.worker import DONE, EventBus

router = APIRouter()


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus


# ------------------------------------------------------------------ health
@router.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    try:
        depth = await runs_repo.queue_depth()
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        depth, database = {}, f"error: {type(exc).__name__}"
    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        llm_provider=settings.llm_provider,
        model=settings.model,
        database=database,
        worker_enabled=settings.enable_worker,
        queue=depth,
    )


# -------------------------------------------------------------------- runs
@router.post("/runs", response_model=CreateRunResponse, status_code=202)
async def create_run(
    body: CreateRunRequest,
    settings: Settings = Depends(get_settings),
) -> CreateRunResponse:
    """只入队，不执行。立刻返回 202，由 worker 异步领取。"""
    repo_path = _resolve_repo_path(body.repo_path, settings)
    row = await runs_repo.create_run(
        task=body.task,
        repo_path=str(repo_path),
        source="manual",
        max_attempts=body.max_attempts or settings.max_attempts,
    )
    return CreateRunResponse(
        run_id=row.id,
        status=row.status,
        events_url=f"/runs/{row.id}/events",
    )


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(
    status: RunStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RunResponse]:
    rows = await runs_repo.list_runs(status=status, limit=min(limit, 200), offset=offset)
    return [RunResponse.from_row(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID) -> RunResponse:
    return RunResponse.from_row(await _require(run_id))


@router.post("/runs/{run_id}/cancel", response_model=RunResponse, status_code=202)
async def cancel_run(run_id: UUID) -> RunResponse:
    await _require(run_id)
    try:
        row = await runs_repo.transition(run_id, RunStatus.CANCELLED)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_row(row)


# ---------------------------------------------------------------- approval
@router.post("/runs/{run_id}/approval", response_model=RunResponse)
async def decide_approval(run_id: UUID, body: ApprovalRequest) -> RunResponse:
    """人工审批闸门。只有 pending_approval 的 run 能被批准/驳回。"""
    await _require(run_id)
    try:
        await approvals_repo.decide(
            run_id,
            decision=body.decision,
            decided_by=body.decided_by,
            reason=body.reason,
        )
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_row(await _require(run_id))


@router.get("/runs/{run_id}/approvals")
async def approval_history(run_id: UUID) -> list[dict]:
    await _require(run_id)
    return [a.model_dump(mode="json") for a in await approvals_repo.history(run_id)]


# --------------------------------------------------------------------- SSE
@router.get("/runs/{run_id}/events")
async def stream_events(run_id: UUID, bus: EventBus = Depends(get_bus)):
    """Server-Sent Events：一个长连接，每个节点完成推一帧 `data: <json>`。"""
    row = await _require(run_id)

    # 已经是终态：没有后续事件了，回放一次就关，别让客户端干等
    if row.status not in ACTIVE:
        queue = bus.subscribe(run_id, replay=True)
        queue.put_nowait(DONE)
    else:
        queue = bus.subscribe(run_id, replay=True)

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if not isinstance(item, RunEvent):  # 哨兵
                    yield "event: done\ndata: {}\n\n"
                    return
                yield f"event: {item.type}\ndata: {item.model_dump_json()}\n\n"
        except asyncio.CancelledError:
            raise  # 客户端断开
        finally:
            bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ 内部
def _resolve_repo_path(raw: str | None, settings: Settings) -> Path:
    """同步函数：两次 stat 调用，别放在 async 处理函数里。"""
    path = Path(raw).expanduser().resolve() if raw else settings.sample_repo
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"仓库路径不存在: {path}")
    return path


async def _require(run_id: UUID) -> RunRow:
    row = await runs_repo.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    return row
