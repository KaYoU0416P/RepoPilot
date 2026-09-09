"""审批流水。

设计取舍：审批结果**不覆盖写**在 runs 表上，而是每次决策追加一行。
理由：审计场景要的是「谁在什么时候基于什么理由做了什么决定」的历史，
只存当前值会丢掉「先驳回、改完再批准」这种真实过程。

runs.status 存的是当前状态（查询快），approvals 存的是过程（可追溯）。
两者由同一个事务写入，保证一致。
"""

from uuid import UUID

from repopilot.db.models import ApprovalRow
from repopilot.db.pool import get_pool, transaction
from repopilot.db.runs import transition
from repopilot.domain import RunStatus
from repopilot.observability import get_logger

log = get_logger(__name__)


async def decide(
    run_id: UUID,
    *,
    decision: str,
    decided_by: str,
    reason: str | None = None,
) -> ApprovalRow:
    """记录一次审批决定，并推动 run 的状态。

    approved → publishing（接着由 publisher 去开 PR）
    rejected → rejected（终态）
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision 只能是 approved / rejected，收到 {decision!r}")

    target = RunStatus.PUBLISHING if decision == "approved" else RunStatus.REJECTED

    # 先流转状态（内含守卫 + 乐观锁）。非法流转会在这里抛出来，
    # 这样不会留下一条「审批了但状态没动」的孤儿记录。
    await transition(run_id, target, expected=RunStatus.PENDING_APPROVAL)

    async with transaction() as conn:
        record = await conn.fetchrow(
            """
            INSERT INTO approvals (run_id, decision, decided_by, reason)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            run_id,
            decision,
            decided_by,
            reason,
        )
    log.info("run=%s 被 %s %s", run_id, decided_by, decision)
    return ApprovalRow.from_record(record)


async def history(run_id: UUID) -> list[ApprovalRow]:
    records = await get_pool().fetch(
        "SELECT * FROM approvals WHERE run_id = $1 ORDER BY decided_at DESC", run_id
    )
    return [ApprovalRow.from_record(r) for r in records]
