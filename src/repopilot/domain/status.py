"""Run 的状态机。

把「什么状态能变成什么状态」写成一张表，而不是散落在各处的 if。
好处：非法流转在一个地方被挡住，且这张表本身可以被测试。

    queued ──▶ running ──▶ pending_approval ──▶ publishing ──▶ published
      │           │               │
      │           ├──▶ failed     └──▶ rejected
      │           └──▶ queued（租约过期，被重新领取）
      └──▶ cancelled
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """StrEnum：既是枚举又是 str，可以直接和数据库里的文本比较。

    Java 对照：enum + 实现了 toString()，但这个连 == "queued" 都成立。
    """

    QUEUED = "queued"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 唯一的真相来源：某状态允许流转到哪些状态。
TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.PENDING_APPROVAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.QUEUED,  # 租约过期 → 退回队列重试
        }
    ),
    RunStatus.PENDING_APPROVAL: frozenset(
        {RunStatus.PUBLISHING, RunStatus.REJECTED, RunStatus.CANCELLED}
    ),
    RunStatus.PUBLISHING: frozenset({RunStatus.PUBLISHED, RunStatus.FAILED}),
    # 终态：进去就出不来
    RunStatus.PUBLISHED: frozenset(),
    RunStatus.REJECTED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

#: 终态 = 没有任何出边的状态。不要手写第二份清单，从 TRANSITIONS 推导。
TERMINAL: frozenset[RunStatus] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

#: 非终态。
ACTIVE: frozenset[RunStatus] = frozenset(TRANSITIONS) - TERMINAL


class InvalidTransition(ValueError):
    """非法状态流转。带上 from/to 方便排查。"""

    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        self.current = current
        self.target = target
        allowed = ", ".join(sorted(TRANSITIONS.get(current, frozenset()))) or "(终态)"
        super().__init__(f"不能从 {current} 变成 {target}；{current} 只允许 → {allowed}")


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    """current 能不能变成 target。

    只回答「能不能」，不抛异常 —— 判断和报错是两件事，报错交给 assert_transition。
    脏数据（数据库里读到不认识的状态）走 .get 的默认值，得到空集合，自然是 False。

    注意自转也是 False：TRANSITIONS[X] 里不含 X。这是刻意的，因为
    transition() 用 `WHERE status = 当前状态` 做乐观锁，允许原地踏步会让它失去意义。
    """
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: RunStatus, target: RunStatus) -> None:
    """守卫版本：不合法就抛 InvalidTransition。所有写库的地方都先过这一关。"""
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
