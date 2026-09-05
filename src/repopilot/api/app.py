"""App factory + lifespan. `uv run uvicorn repopilot.api.app:app --reload`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from repopilot.api.routes import router
from repopilot.api.service import RunService
from repopilot.config import get_settings
from repopilot.observability import get_logger, setup_logging

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup before `yield`, shutdown after. Replaces @app.on_event."""
    setup_logging()
    settings = get_settings()
    app.state.run_service = RunService(settings)
    log.info("RepoPilot up | provider=%s model=%s", settings.llm_provider, settings.model)
    yield
    log.info("RepoPilot shutting down")


def create_app() -> FastAPI:
    app = FastAPI(title="RepoPilot", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
