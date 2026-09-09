"""HTTP 层测试。用 ASGI transport，不开真实端口、不起 uvicorn。"""

from uuid import UUID

import pytest

from repopilot.db import runs as runs_repo
from repopilot.domain import RunStatus

# client fixture 在 conftest.py，webhook 测试也用它。
pytestmark = pytest.mark.usefixtures("db")


# ------------------------------------------------------------------ health
async def test_health_reports_database_and_queue(client):
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "queue" in body


# -------------------------------------------------------------------- 入队
async def test_create_run_enqueues_and_returns_202(client):
    response = await client.post("/runs", json={"task": "修一下除零的 bug"})
    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "queued"
    assert body["events_url"] == f"/runs/{body['run_id']}/events"

    # 真的落库了，而不是只在内存里
    row = await runs_repo.get_run(body["run_id"])
    assert row is not None
    assert row.status == RunStatus.QUEUED


async def test_validation_rejects_short_task(client):
    assert (await client.post("/runs", json={"task": "x"})).status_code == 422


async def test_bad_repo_path_is_400(client):
    response = await client.post(
        "/runs", json={"task": "做点什么", "repo_path": "/nope/nowhere"}
    )
    assert response.status_code == 400


async def test_unknown_run_is_404(client):
    assert (
        await client.get("/runs/00000000-0000-0000-0000-000000000000")
    ).status_code == 404


async def test_list_runs_filters_by_status(client):
    await client.post("/runs", json={"task": "任务甲"})
    await client.post("/runs", json={"task": "任务乙"})

    queued = (await client.get("/runs", params={"status": "queued"})).json()
    assert len(queued) == 2

    running = (await client.get("/runs", params={"status": "running"})).json()
    assert running == []


# -------------------------------------------------------------- 审批闸门
async def test_cannot_approve_a_run_that_is_still_queued(client):
    """审批闸门的核心：没跑完的任务不能批准。返回 409 而不是 500。"""
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]

    response = await client.post(
        f"/runs/{run_id}/approval",
        json={"decision": "approved", "decided_by": "kayou"},
    )
    assert response.status_code == 409


async def test_approve_moves_run_to_publishing(client):
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    await _force_status(run_id, RunStatus.PENDING_APPROVAL)

    response = await client.post(
        f"/runs/{run_id}/approval",
        json={"decision": "approved", "decided_by": "kayou", "reason": "看过 diff 了"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "publishing"

    history = (await client.get(f"/runs/{run_id}/approvals")).json()
    assert len(history) == 1
    assert history[0]["decided_by"] == "kayou"
    assert history[0]["reason"] == "看过 diff 了"


async def test_reject_is_terminal(client):
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    await _force_status(run_id, RunStatus.PENDING_APPROVAL)

    rejected = await client.post(
        f"/runs/{run_id}/approval",
        json={"decision": "rejected", "decided_by": "kayou", "reason": "改错地方了"},
    )
    assert rejected.json()["status"] == "rejected"

    # 驳回是终态，不能再批准
    again = await client.post(
        f"/runs/{run_id}/approval",
        json={"decision": "approved", "decided_by": "kayou"},
    )
    assert again.status_code == 409


async def test_double_approval_is_rejected(client):
    """幂等的另一面：同一个 run 不能被批准两次，否则会开出两个 PR。"""
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    await _force_status(run_id, RunStatus.PENDING_APPROVAL)

    body = {"decision": "approved", "decided_by": "kayou"}
    assert (await client.post(f"/runs/{run_id}/approval", json=body)).status_code == 200
    assert (await client.post(f"/runs/{run_id}/approval", json=body)).status_code == 409


async def test_invalid_decision_is_422(client):
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    response = await client.post(
        f"/runs/{run_id}/approval", json={"decision": "maybe", "decided_by": "kayou"}
    )
    assert response.status_code == 422


# -------------------------------------------------------------------- 取消
async def test_cancel_queued_run(client):
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    response = await client.post(f"/runs/{run_id}/cancel")
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"


async def test_cannot_cancel_a_published_run(client):
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    await _force_status(run_id, RunStatus.PUBLISHED)

    assert (await client.post(f"/runs/{run_id}/cancel")).status_code == 409


# --------------------------------------------------------------------- SSE
async def test_sse_on_a_finished_run_closes_immediately(client):
    """终态的 run 没有后续事件，流要立刻结束，不能让客户端一直挂着。"""
    run_id = (await client.post("/runs", json={"task": "修 bug"})).json()["run_id"]
    await client.post(f"/runs/{run_id}/cancel")

    lines = []
    async with client.stream("GET", f"/runs/{run_id}/events", timeout=10) as stream:
        async for line in stream.aiter_lines():
            lines.append(line)
    assert any("event: done" in line for line in lines)


#: 正常路径。测试要把 run 推到某个状态时，必须沿着合法边一步步走 ——
#: 状态机不给「测试后门」，这本身就是它的价值。
_HAPPY_PATH = [
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PENDING_APPROVAL,
    RunStatus.PUBLISHING,
    RunStatus.PUBLISHED,
]


async def _force_status(run_id: str, target: RunStatus) -> None:
    """测试专用：沿合法路径把 run 推到 target，跳过真正跑 Agent。"""
    uid = UUID(run_id)
    stop = _HAPPY_PATH.index(target)
    while True:
        row = await runs_repo.get_run(uid)
        current = _HAPPY_PATH.index(row.status)
        if current >= stop:
            return
        await runs_repo.transition(uid, _HAPPY_PATH[current + 1])
