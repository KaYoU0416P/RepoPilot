"""HTTP surface. Handlers stay thin: validate -> call RunService -> shape response."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from repopilot.api.schemas import (
    CreateRunRequest,
    CreateRunResponse,
    RunEvent,
    RunResponse,
)
from repopilot.api.service import RunRecord, RunService
from repopilot.config import Settings, get_settings

router = APIRouter()


def get_service(request: Request) -> RunService:
    """FastAPI dependency: pull the singleton the lifespan put on app.state."""
    return request.app.state.run_service


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict:
    return {"status": "ok", "llm_provider": settings.llm_provider, "model": settings.model}


@router.post("/runs", response_model=CreateRunResponse, status_code=202)
async def create_run(
    body: CreateRunRequest,
    service: RunService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> CreateRunResponse:
    repo_path = _resolve_repo_path(body.repo_path, settings)
    record = await service.start(
        task=body.task,
        repo_path=repo_path,
        max_retries=body.max_retries if body.max_retries is not None else settings.max_retries,
    )
    return CreateRunResponse(
        run_id=record.run_id,
        status=record.status,
        events_url=f"/runs/{record.run_id}/events",
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, service: RunService = Depends(get_service)) -> RunResponse:
    return service.to_response(_require(service, run_id))


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, service: RunService = Depends(get_service)) -> dict:
    _require(service, run_id)
    return {"cancelled": await service.cancel(run_id)}


@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str, service: RunService = Depends(get_service)):
    """Server-Sent Events: one long-lived HTTP response, `data: <json>\\n\\n` per event."""
    record = _require(service, run_id)
    queue = await service.subscribe(record)

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if not isinstance(item, RunEvent):  # sentinel
                    yield "event: done\ndata: {}\n\n"
                    return
                yield f"event: {item.type}\ndata: {item.model_dump_json()}\n\n"
        except asyncio.CancelledError:
            # Client disconnected. Drop the subscriber so the run doesn't keep a
            # queue growing forever.
            raise
        finally:
            service.unsubscribe(record, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_repo_path(raw: str | None, settings: Settings) -> Path:
    """Sync on purpose: two cheap stat() calls, kept out of the async handler."""
    path = Path(raw).expanduser().resolve() if raw else settings.sample_repo
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path not found: {path}")
    return path


def _require(service: RunService, run_id: str) -> RunRecord:
    record = service.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return record
