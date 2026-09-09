import os

import asyncpg
import pytest

from repopilot.config import PROJECT_ROOT, get_settings
from repopilot.db.pool import close_pool, init_pool, read_schema
from repopilot.tools import build_registry
from repopilot.workspace import WorkspaceManager

# 测试绝不联网、绝不花 token。
os.environ["REPOPILOT_LLM_PROVIDER"] = "scripted"
get_settings.cache_clear()

ADMIN_DSN = "postgresql://repopilot:repopilot@localhost:5433/repopilot"
TEST_DSN = os.environ.get("REPOPILOT_TEST_DSN", "postgresql://repopilot:repopilot@localhost:5433/repopilot_test")
SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

_TABLES = ("approvals", "webhook_deliveries", "runs")


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def workspace(settings, tmp_path):
    """fixtures/sample_repo 的一次性副本，已 git init，测完删掉。"""
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create(settings.sample_repo, run_id="test")
    yield ws
    manager.cleanup(ws)


@pytest.fixture
def registry():
    return build_registry()


async def _ensure_test_database() -> None:
    """测试库不存在就建一个并灌 schema。第一次跑测试时自动完成。"""
    try:
        conn = await asyncpg.connect(TEST_DSN)
    except asyncpg.InvalidCatalogNameError:
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute("CREATE DATABASE repopilot_test")
        finally:
            await admin.close()
        conn = await asyncpg.connect(TEST_DSN)

    try:
        exists = await conn.fetchval("SELECT to_regclass('public.runs')")
        if exists is None:
            await conn.execute(read_schema(SCHEMA_FILE))
    finally:
        await conn.close()


@pytest.fixture
async def db():
    """给每个用例一个干净的数据库。

    每个用例自己建连接池：pytest-asyncio 默认每个用例一个 event loop，
    而 asyncpg 的连接是绑定 loop 的，跨 loop 复用会炸。
    """
    await _ensure_test_database()
    pool = await init_pool(TEST_DSN, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture
async def client(db, monkeypatch):
    """走 ASGI transport 的 HTTP 客户端，不开端口、不起 uvicorn。

    关掉 worker：否则它会把刚入队的任务领走，断言 status == 'queued' 就变成
    竞态。测 HTTP 层就只测 HTTP 层。
    """
    import httpx

    from repopilot.api.app import create_app
    from repopilot.worker import EventBus

    monkeypatch.setattr(get_settings(), "enable_worker", False)

    app = create_app()
    app.state.bus = EventBus()
    app.state.worker = None
    app.state.worker_task = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def pytest_collection_modifyitems(config, items):
    """数据库没起来时，把 db 相关用例标成 skip 而不是一片红。"""
    import socket

    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect(("localhost", 5433))
        return
    except OSError:
        pass
    finally:
        sock.close()

    skip = pytest.mark.skip(reason="Postgres 没起来，先跑 `make db-up`")
    for item in items:
        if "db" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)
