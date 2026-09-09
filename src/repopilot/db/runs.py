"""runs 表的数据访问层。

这张表同时是「业务实体」和「任务队列」。用一张表做队列的关键点：
  - 领取任务必须是原子的，否则两个 worker 会抢到同一行
  - worker 崩了任务必须能被回收，靠租约（lease_expires_at）而不是靠进程活着
"""

from typing import Any
from uuid import UUID

import asyncpg

from repopilot.db.models import RunRow
from repopilot.db.pool import get_pool, transaction
from repopilot.domain import RunStatus, assert_transition
from repopilot.observability import get_logger

log = get_logger(__name__)


# ============================================================ 写入 / 入队
async def create_run(
    *,
    task: str,
    repo_path: str,
    source: str = "manual",
    external_ref: str | None = None,
    max_attempts: int = 3,
    conn: asyncpg.Connection | None = None,
) -> RunRow:
    """入队一个新任务。状态直接是 queued，等 worker 来领。"""
    sql = """
        INSERT INTO runs (task, repo_path, source, external_ref, max_attempts)
        VALUES ($1, $2, $3::trigger_source, $4, $5)
        RETURNING *
    """
    executor = conn or get_pool()
    record = await executor.fetchrow(sql, task, repo_path, source, external_ref, max_attempts)
    return RunRow.from_record(record)


# ================================================================== 出队
#: worker 领取任务的 SQL。**这是整个项目最值得你背下来的一段 SQL。**
#: 契约见 tests/test_queue.py。写法见 docs/learning.md「PG 当队列用」。
CLAIM_SQL = """
    -- TODO(你来写)
    -- $1 = worker_id, $2 = lease_seconds
"""


async def claim_next_run(worker_id: str, lease_seconds: int = 120) -> RunRow | None:
    """原子地领取一个待办任务；没有就返回 None。

    要领到两种任务：
      1. status = 'queued' 的新任务
      2. status = 'running' 但 lease_expires_at < now() 的僵尸任务
         （持有它的 worker 已经崩了）

    领取时必须同时：
      - status 改成 'running'
      - attempts + 1
      - locked_by = worker_id
      - lease_expires_at = now() + lease_seconds
      - started_at 首次领取时才设置（用 COALESCE 保留原值）
    """
    if "TODO" in CLAIM_SQL:
        raise NotImplementedError("CLAIM_SQL 是你的手写任务，见 db/runs.py")

    record = await get_pool().fetchrow(CLAIM_SQL, worker_id, lease_seconds)
    if record is None:
        return None
    row = RunRow.from_record(record)
    log.info("worker=%s 领取 run=%s (第 %s 次尝试)", worker_id, row.id, row.attempts)
    return row


async def heartbeat(run_id: UUID, worker_id: str, lease_seconds: int = 120) -> bool:
    """续租。Agent 跑得久时定期调用，防止任务被别的 worker 抢走。

    `locked_by = $2` 这个条件很重要：如果租约已经被别人抢走，续租必须失败，
    让当前 worker 知道自己已经失去所有权。
    """
    sql = """
        UPDATE runs
           SET lease_expires_at = now() + make_interval(secs => $3)
         WHERE id = $1 AND locked_by = $2 AND status = 'running'
    """
    result = await get_pool().execute(sql, run_id, worker_id, lease_seconds)
    return result.endswith(" 1")


async def reap_exhausted() -> int:
    """把重试次数用尽的僵尸任务标记为失败，避免它们永远在队列里打转。"""
    sql = """
        UPDATE runs
           SET status = 'failed',
               error = COALESCE(error, 'worker 失联且重试次数已用尽'),
               finished_at = now()
         WHERE status = 'running'
           AND lease_expires_at < now()
           AND attempts >= max_attempts
    """
    result = await get_pool().execute(sql)
    count = int(result.rsplit(" ", 1)[-1])
    if count:
        log.warning("回收了 %s 个重试用尽的僵尸任务", count)
    return count


# ============================================================== 状态流转
async def transition(
    run_id: UUID,
    target: RunStatus,
    *,
    expected: RunStatus | None = None,
    **fields: Any,
) -> RunRow:
    """带守卫的状态流转。

    两层保护：
      1. 应用层：assert_transition 挡掉业务上非法的流转
      2. 数据库层：UPDATE ... WHERE status = <当前状态> 做乐观锁，
         挡掉「读到写之间别人改了」的并发问题

    Java 对照：第 2 层就是 JPA 的 @Version 乐观锁，只是这里用 status 本身当版本号。
    """
    async with transaction() as conn:
        current_record = await conn.fetchrow(
            "SELECT status FROM runs WHERE id = $1 FOR UPDATE", run_id
        )
        if current_record is None:
            raise LookupError(f"run 不存在: {run_id}")

        current = RunStatus(current_record["status"])
        if expected is not None and current != expected:
            raise ValueError(f"run {run_id} 期望是 {expected}，实际是 {current}")

        assert_transition(current, target)

        sets = ["status = $2::run_status"]
        args: list[Any] = [run_id, target.value]
        for i, (column, value) in enumerate(fields.items(), start=3):
            sets.append(f"{column} = ${i}")
            args.append(value)

        guard = len(args) + 1  # 乐观锁条件的占位符编号
        record = await conn.fetchrow(
            f"UPDATE runs SET {', '.join(sets)} "
            f"WHERE id = $1 AND status = ${guard}::run_status RETURNING *",
            *args,
            current.value,
        )
        if record is None:
            raise RuntimeError(f"run {run_id} 状态被并发修改了")

        log.info("run=%s %s → %s", run_id, current, target)
        return RunRow.from_record(record)


# ================================================================== 查询
async def get_run(run_id: UUID) -> RunRow | None:
    record = await get_pool().fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
    return RunRow.from_record(record) if record else None


async def list_runs(
    *, status: RunStatus | None = None, limit: int = 50, offset: int = 0
) -> list[RunRow]:
    if status is None:
        sql = "SELECT * FROM runs ORDER BY created_at DESC LIMIT $1 OFFSET $2"
        records = await get_pool().fetch(sql, limit, offset)
    else:
        sql = """
            SELECT * FROM runs
             WHERE status = $1::run_status
             ORDER BY created_at DESC
             LIMIT $2 OFFSET $3
        """
        records = await get_pool().fetch(sql, status.value, limit, offset)
    return [RunRow.from_record(r) for r in records]


async def find_by_external_ref(external_ref: str) -> RunRow | None:
    """同一个 Issue 已经有在跑的任务了吗？—— 业务层面的第二道幂等。"""
    sql = """
        SELECT * FROM runs
         WHERE external_ref = $1
           AND status NOT IN ('failed', 'cancelled', 'rejected')
         ORDER BY created_at DESC
         LIMIT 1
    """
    record = await get_pool().fetchrow(sql, external_ref)
    return RunRow.from_record(record) if record else None


async def queue_depth() -> dict[str, int]:
    """按状态统计，给 /health 和监控用。"""
    records = await get_pool().fetch("SELECT status::text AS s, count(*) AS n FROM runs GROUP BY 1")
    return {r["s"]: r["n"] for r in records}
