"""幂等去重的契约 —— CLAIM_DELIVERY_SQL 的验收标准。

需要 Postgres：先 `make db-up`。
"""

import asyncio

import pytest

from repopilot.db import deliveries

pytestmark = pytest.mark.usefixtures("db")


async def test_first_delivery_is_accepted():
    assert await deliveries.claim_delivery("d-1", source="github") is True


async def test_replay_of_the_same_delivery_is_rejected():
    assert await deliveries.claim_delivery("d-1", source="github") is True
    assert await deliveries.claim_delivery("d-1", source="github") is False
    assert await deliveries.claim_delivery("d-1", source="github") is False


async def test_different_delivery_ids_are_independent():
    assert await deliveries.claim_delivery("d-1", source="github") is True
    assert await deliveries.claim_delivery("d-2", source="github") is True


async def test_payload_is_persisted():
    await deliveries.claim_delivery(
        "d-1",
        source="github",
        event_type="issues",
        payload={"action": "labeled", "number": 42},
    )
    row = await deliveries.get_delivery("d-1")
    assert row["event_type"] == "issues"
    assert row["payload"]["number"] == 42


async def test_replay_does_not_overwrite_the_original_payload():
    """重投的 payload 可能被截断或不完整，绝不能覆盖第一次的记录。"""
    await deliveries.claim_delivery("d-1", source="github", payload={"v": "原始"})
    await deliveries.claim_delivery("d-1", source="github", payload={"v": "重投"})

    row = await deliveries.get_delivery("d-1")
    assert row["payload"]["v"] == "原始"


async def test_concurrent_duplicates_only_one_wins():
    """真正的考点：20 个并发的同一投递，必须只有 1 个返回 True。

    先 SELECT 再 INSERT 的写法会在这里挂掉 —— 多个协程会同时读到「不存在」。
    只有把判断和插入交给数据库的唯一约束原子完成才是对的。
    """
    results = await asyncio.gather(
        *(deliveries.claim_delivery("同一个投递", source="github") for _ in range(20))
    )
    assert sum(results) == 1


async def test_attach_run_links_delivery_to_the_created_run():
    from repopilot.db import runs as runs_repo

    await deliveries.claim_delivery("d-1", source="github")
    row = await runs_repo.create_run(task="修 bug", repo_path="/tmp/fake")
    await deliveries.attach_run("d-1", row.id)

    assert (await deliveries.get_delivery("d-1"))["run_id"] == row.id
