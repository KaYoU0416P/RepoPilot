"""HTTP-level tests using an ASGI transport - no real socket, no uvicorn."""

import asyncio
import json

import httpx
import pytest

from repopilot.api.app import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_create_run_returns_202_immediately(client):
    response = await client.post("/runs", json={"task": "fix the divide bug"})
    assert response.status_code == 202
    body = response.json()
    assert body["run_id"]
    assert body["events_url"] == f"/runs/{body['run_id']}/events"


async def test_validation_rejects_short_task(client):
    assert (await client.post("/runs", json={"task": "x"})).status_code == 422


async def test_unknown_run_is_404(client):
    assert (await client.get("/runs/deadbeef")).status_code == 404


async def test_bad_repo_path_is_400(client):
    response = await client.post(
        "/runs", json={"task": "do something", "repo_path": "/nope/nowhere"}
    )
    assert response.status_code == 400


@pytest.mark.slow
async def test_sse_stream_reports_every_node(client):
    created = (await client.post("/runs", json={"task": "fix divide by zero"})).json()
    run_id = created["run_id"]

    nodes: list[str] = []
    async with client.stream("GET", f"/runs/{run_id}/events", timeout=120) as stream:
        async for line in stream.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            if not payload:
                break
            if payload.get("type") == "node_completed":
                nodes.append(payload["node"])
            if payload.get("type") in ("run_finished", "run_error"):
                break

    assert nodes[:4] == ["analyze", "plan", "execute", "run_tests"]
    assert "finish" in nodes

    final = (await client.get(f"/runs/{run_id}")).json()
    assert final["status"] == "succeeded"
    assert final["evaluation"]["task_success"] is True


@pytest.mark.slow
async def test_cancel_marks_the_run_as_error(client):
    run_id = (await client.post("/runs", json={"task": "fix divide by zero"})).json()["run_id"]
    await asyncio.sleep(0)
    assert (await client.post(f"/runs/{run_id}/cancel")).status_code == 202
