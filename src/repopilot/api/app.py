"""应用装配 + 生命周期。

启动顺序：日志 → 连接池 → 事件总线 → worker 后台任务 → 接客
停机顺序反过来：停止领新任务 → 等在跑的收尾 → 关连接池
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from repopilot.api.routes import router
from repopilot.config import get_settings
from repopilot.db import close_pool, init_pool
from repopilot.observability import get_logger, setup_logging, setup_tracing
from repopilot.worker import EventBus, Worker

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    settings = get_settings()
    tracing = setup_tracing(
        enabled=settings.otel_enabled, service_name=settings.otel_service_name
    )

    await init_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )

    app.state.bus = EventBus()
    app.state.worker = None
    app.state.worker_task = None

    if settings.enable_worker:
        worker = Worker(settings, app.state.bus)
        app.state.worker = worker
        app.state.worker_task = asyncio.create_task(worker.run_forever(), name="worker")
        log.info("worker 已随 API 进程启动 (enable_worker=true)")

    log.info(
        "RepoPilot 就绪 | provider=%s model=%s db=%s trace=%s",
        settings.llm_provider,
        settings.model,
        settings.database_url.rsplit("@", 1)[-1],
        "on" if tracing else "off",
    )
    try:
        yield
    finally:
        if app.state.worker is not None:
            await app.state.worker.stop()
        if app.state.worker_task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    app.state.worker_task, timeout=settings.shutdown_grace_seconds + 5
                )
        await close_pool()
        log.info("RepoPilot 已停止")


def create_app() -> FastAPI:
    app = FastAPI(title="RepoPilot", version="0.2.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
