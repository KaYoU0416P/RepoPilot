"""应用装配 + 生命周期。

启动顺序：日志 → 连接池 → 事件总线 → worker 后台任务 → 接客
停机顺序反过来：停止领新任务 → 等在跑的收尾 → 关连接池
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from repopilot.api.routes import public, router
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

    # 鉴权是 fail closed 的：没配 key = 所有业务接口 401。那本身是对的，
    # 但**沉默地对**会让人对着一片 401 查上半天。启动时就喊出来。
    if not settings.api_keys:
        log.warning(
            "没有配置 REPOPILOT_API_KEYS —— 除 /health 和 /webhooks/github 外"
            "所有接口都会返回 401。本地开发请在 .env 里配一把（见 .env.example）"
        )
    else:
        log.info(
            "API 鉴权已启用 | %s",
            ", ".join(f"{k.name}({'+'.join(k.scopes)})" for k in settings.api_keys),
        )

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
    # 顺序不影响路由匹配，但**先列公开的**是刻意的：读这个文件的人
    # 第一眼看到的应该是"哪些接口不用鉴权"，而不是去数哪些用了。
    app.include_router(public)
    app.include_router(router)
    return app


app = create_app()
