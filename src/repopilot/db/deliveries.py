"""webhook 幂等台账。

场景：GitHub 投递 webhook 时如果没在 10 秒内收到 2xx，会重投最多 3 次。
网络抖动、你的服务重启、处理慢 —— 都会导致同一个事件被投递多次。
如果不去重，一个 Issue 会开出 3 个 PR。

去重的锚点是 GitHub 给的 X-GitHub-Delivery header：同一次事件重投时它不变。
"""

from typing import Any
from uuid import UUID

import asyncpg

from repopilot.db.pool import get_pool
from repopilot.observability import get_logger

log = get_logger(__name__)


#: 幂等去重的 SQL。**这是第二段值得你背下来的 SQL。**
#: 契约见 tests/test_idempotency.py。
CLAIM_DELIVERY_SQL = """
    -- TODO(你来写)
    -- $1 = delivery_id, $2 = source, $3 = event_type, $4 = payload(jsonb)
    -- 第一次插入要 RETURNING 出点东西；重复投递要一行都不返回。
"""


async def claim_delivery(
    delivery_id: str,
    *,
    source: str,
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
    conn: asyncpg.Connection | None = None,
) -> bool:
    """尝试登记一次投递。

    返回 True  = 第一次见到，调用方应该继续处理
    返回 False = 重复投递，调用方应该直接返回 200 并且**什么都不做**

    要点：判断「是不是第一次」和「插入记录」必须是**同一个原子操作**。
    先 SELECT 再 INSERT 是经典错误 —— 两个并发请求会同时 SELECT 到空，
    然后都以为自己是第一次。数据库的唯一约束是唯一可靠的仲裁者。
    """
    if "TODO" in CLAIM_DELIVERY_SQL:
        raise NotImplementedError("CLAIM_DELIVERY_SQL 是你的手写任务，见 db/deliveries.py")

    executor = conn or get_pool()
    record = await executor.fetchrow(
        CLAIM_DELIVERY_SQL, delivery_id, source, event_type, payload or {}
    )
    is_new = record is not None
    if not is_new:
        log.info("重复投递，已忽略: %s", delivery_id)
    return is_new


async def attach_run(delivery_id: str, run_id: UUID) -> None:
    """把投递和它创建出来的 run 关联起来，方便事后追溯。"""
    await get_pool().execute(
        "UPDATE webhook_deliveries SET run_id = $2 WHERE delivery_id = $1",
        delivery_id,
        run_id,
    )


async def get_delivery(delivery_id: str) -> dict[str, Any] | None:
    record = await get_pool().fetchrow(
        "SELECT * FROM webhook_deliveries WHERE delivery_id = $1", delivery_id
    )
    return dict(record) if record else None
