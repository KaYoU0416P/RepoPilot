"""状态机的契约。

这些用例现在是红的，直到你实现 domain/status.py 里的 can_transition。
不需要数据库，跑得很快：

    uv run pytest tests/test_status.py -v
"""

import pytest

from repopilot.domain import (
    ACTIVE,
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    RunStatus,
    assert_transition,
    can_transition,
)


# ---------------------------------------------------------------- 正常流转
@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.PENDING_APPROVAL),
        (RunStatus.RUNNING, RunStatus.QUEUED),  # 租约过期退回队列
        (RunStatus.PENDING_APPROVAL, RunStatus.PUBLISHING),
        (RunStatus.PENDING_APPROVAL, RunStatus.REJECTED),
        (RunStatus.PUBLISHING, RunStatus.PUBLISHED),
    ],
)
def test_allowed(current, target):
    assert can_transition(current, target) is True


# ---------------------------------------------------------------- 非法流转
@pytest.mark.parametrize(
    ("current", "target"),
    [
        # 不能跳过审批直接发布 —— 这是整个审批闸门存在的意义
        (RunStatus.RUNNING, RunStatus.PUBLISHED),
        (RunStatus.QUEUED, RunStatus.PENDING_APPROVAL),
        # 终态不能复活
        (RunStatus.PUBLISHED, RunStatus.QUEUED),
        (RunStatus.FAILED, RunStatus.RUNNING),
        (RunStatus.REJECTED, RunStatus.PUBLISHING),
        (RunStatus.CANCELLED, RunStatus.QUEUED),
    ],
)
def test_forbidden(current, target):
    assert can_transition(current, target) is False


def test_self_transition_is_forbidden():
    """原地踏步不算合法流转，否则乐观锁会失去意义。"""
    for status in RunStatus:
        assert can_transition(status, status) is False


def test_dirty_data_returns_false_not_raise():
    """数据库里读到不认识的状态时，返回 False，不要抛异常。"""
    assert can_transition("不存在的状态", RunStatus.RUNNING) is False  # type: ignore[arg-type]


# ------------------------------------------------------------ 表本身的性质
def test_every_status_appears_in_the_table():
    assert set(TRANSITIONS) == set(RunStatus)


def test_terminal_states_are_exactly_the_ones_without_exits():
    assert TERMINAL == {
        RunStatus.PUBLISHED,
        RunStatus.REJECTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
    assert ACTIVE == set(RunStatus) - TERMINAL


def test_every_active_status_can_reach_a_terminal_state():
    """不能存在「进去就卡死」的状态。用 BFS 验证可达性。"""
    for start in ACTIVE:
        seen, frontier = {start}, [start]
        while frontier:
            node = frontier.pop()
            if node in TERMINAL:
                break
            for nxt in TRANSITIONS[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        else:
            pytest.fail(f"{start} 到不了任何终态")


# ------------------------------------------------------------------ 守卫
def test_assert_transition_passes_silently_when_legal():
    assert assert_transition(RunStatus.QUEUED, RunStatus.RUNNING) is None


def test_assert_transition_raises_with_a_useful_message():
    with pytest.raises(InvalidTransition) as exc_info:
        assert_transition(RunStatus.RUNNING, RunStatus.PUBLISHED)

    exc = exc_info.value
    assert exc.current == RunStatus.RUNNING
    assert exc.target == RunStatus.PUBLISHED
    # 报错信息要告诉人「那我能去哪」，否则排查全靠猜
    assert "pending_approval" in str(exc)
