"""asyncpg 连接池的生命周期管理。

Java 对照：HikariCP。asyncpg 的 Pool 同样是「借出-归还」模型，
但借出的是协程级别的连接，不占线程。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from repopilot.observability import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """进程启动时调一次。"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            # asyncpg 默认把 jsonb 解成 str，这里注册成 dict/list，省得到处 json.loads
            init=_register_codecs,
        )
        log.info("pg pool ready (min=%s max=%s)", min_size, max_size)
    return _pool


async def _register_codecs(conn: asyncpg.Connection) -> None:
    import json

    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("连接池还没初始化，先调 init_pool()")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("pg pool closed")


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """一个事务边界。

        async with transaction() as conn:
            await conn.execute(...)

    退出时自动 commit；抛异常自动 rollback。
    Java 对照：@Transactional，但边界是显式的、看得见的。
    """
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        yield conn


def read_schema(schema_file: Path) -> str:
    """同步读一次 DDL 文件，别在协程里做阻塞 IO。"""
    return schema_file.read_text(encoding="utf-8")


async def apply_schema(dsn: str, schema_file: Path) -> None:
    """给测试用：往一个空库里灌 schema.sql。生产走 docker-entrypoint。"""
    ddl = read_schema(schema_file)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(ddl)
    finally:
        await conn.close()
