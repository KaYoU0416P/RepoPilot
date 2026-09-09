"""任务队列的契约 —— 这是 CLAIM_SQL 的验收标准。

需要 Postgres：先 `make db-up`。

    uv run pytest tests/test_queue.py -v
"""

import asyncio

import pytest

from repopilot.db import runs as runs_repo
from repopilot.domain import RunStatus

pytestmark = pytest.mark.usefixtures("db")


async def _enqueue(task: str = "修个 bug", **kw):
    return await runs_repo.create_run(task=task, repo_path="/tmp/fake", **kw)


# ---------------------------------------------------------------- 基本行为
async def test_empty_queue_returns_none():
    assert await runs_repo.claim_next_run("w1") is None


async def test_claim_marks_running_and_stamps_owner():
    created = await _enqueue()
    claimed = await runs_repo.claim_next_run("w1", lease_seconds=60)

    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.status == RunStatus.RUNNING
    assert claimed.locked_by == "w1"
    assert claimed.attempts == 1
    assert claimed.lease_expires_at is not None
    assert claimed.started_at is not None


async def test_fifo_order():
    first = await _enqueue("先来的")
    second = await _enqueue("后到的")

    assert (await runs_repo.claim_next_run("w1")).id == first.id
    assert (await runs_repo.claim_next_run("w1")).id == second.id


async def test_a_claimed_run_is_not_handed_out_again():
    await _enqueue()
    assert await runs_repo.claim_next_run("w1") is not None
    assert await runs_repo.claim_next_run("w2") is None


# ------------------------------------------------------- 并发（核心正确性）
async def test_concurrent_workers_never_get_the_same_run():
    """SKIP LOCKED 的意义：10 个 worker 抢 5 个任务，必须刚好各拿到不同的一个。

    没有 SKIP LOCKED 的话，要么互相阻塞排队（慢），要么抢到同一行（重复执行）。
    """
    for i in range(5):
        await _enqueue(f"任务 {i}")

    claimed = await asyncio.gather(*(runs_repo.claim_next_run(f"w{i}") for i in range(10)))

    got = [row for row in claimed if row is not None]
    assert len(got) == 5
    assert len({row.id for row in got}) == 5  # 无重复
    assert len({row.locked_by for row in got}) == 5  # 各归各的 worker


# ------------------------------------------------------------ 租约 / 可靠性
async def test_expired_lease_is_reclaimed_by_another_worker():
    """worker 崩了不能让任务永远卡住。"""
    created = await _enqueue()
    await runs_repo.claim_next_run("崩掉的worker", lease_seconds=1)

    assert await runs_repo.claim_next_run("接手的worker") is None  # 租约还在
    await asyncio.sleep(1.2)

    reclaimed = await runs_repo.claim_next_run("接手的worker")
    assert reclaimed is not None
    assert reclaimed.id == created.id
    assert reclaimed.locked_by == "接手的worker"
    assert reclaimed.attempts == 2  # 第二次尝试


async def test_started_at_is_kept_across_reclaims():
    """attempts 要累加，但 started_at 记录的是第一次开始的时间，不能被覆盖。"""
    await _enqueue()
    first = await runs_repo.claim_next_run("w1", lease_seconds=1)
    await asyncio.sleep(1.2)
    second = await runs_repo.claim_next_run("w2", lease_seconds=1)

    assert second.started_at == first.started_at


async def test_heartbeat_extends_the_lease():
    created = await _enqueue()
    claimed = await runs_repo.claim_next_run("w1", lease_seconds=1)

    assert await runs_repo.heartbeat(created.id, "w1", lease_seconds=60) is True
    await asyncio.sleep(1.2)
    assert await runs_repo.claim_next_run("w2") is None  # 续租成功，抢不走

    refreshed = await runs_repo.get_run(created.id)
    assert refreshed.lease_expires_at > claimed.lease_expires_at


async def test_heartbeat_fails_if_lease_was_stolen():
    """失去所有权的 worker 必须知道自己失去了所有权。"""
    created = await _enqueue()
    await runs_repo.claim_next_run("原主人", lease_seconds=1)
    await asyncio.sleep(1.2)
    await runs_repo.claim_next_run("新主人", lease_seconds=60)

    assert await runs_repo.heartbeat(created.id, "原主人") is False


async def test_terminal_runs_are_never_claimed():
    created = await _enqueue()
    await runs_repo.transition(created.id, RunStatus.CANCELLED)
    assert await runs_repo.claim_next_run("w1") is None


async def test_reaper_fails_runs_that_used_up_all_attempts():
    created = await _enqueue(max_attempts=1)
    await runs_repo.claim_next_run("崩掉的worker", lease_seconds=1)
    await asyncio.sleep(1.2)

    assert await runs_repo.reap_exhausted() == 1
    row = await runs_repo.get_run(created.id)
    assert row.status == RunStatus.FAILED
    assert "重试次数已用尽" in row.error
