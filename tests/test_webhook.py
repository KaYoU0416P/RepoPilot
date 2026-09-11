"""GitHub webhook 的契约 —— verify_signature 的验收标准。

分两层：
  纯函数层  验签、事件解析，不连库不起服务
  HTTP 层   验签失败的状态码、幂等重投、入队后的落库结果
"""

import json

import pytest

from repopilot.config import get_settings
from repopilot.db import deliveries as deliveries_repo
from repopilot.db import runs as runs_repo
from repopilot.domain import RunStatus
from repopilot.github import extract_issue_trigger, sign, verify_signature

SECRET = "测试用的密钥-not-a-real-secret"


# ------------------------------------------------------------ 造 payload
def issue_payload(*, action="labeled", labels=("repopilot",), number=42, **extra):
    payload = {
        "action": action,
        "issue": {
            "number": number,
            "title": "divide 在除数为 0 时崩溃",
            "body": "复现：calculator.divide(1, 0) 抛 ZeroDivisionError。",
            "labels": [{"name": name} for name in labels],
        },
        "repository": {"full_name": "kayou/demo-repo"},
    }
    payload.update(extra)
    return payload


def post_webhook(client, payload, *, delivery="d-1", event="issues", secret=SECRET, tamper=False):
    """按 GitHub 的方式发一次投递：先序列化成 bytes，再对 bytes 签名。"""
    body = json.dumps(payload).encode()
    signature = sign(secret, body)
    if tamper:
        body = body.replace(b"kayou", b"attacker")  # 签完再改 → 签名必须失效
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )


# =========================================================== 纯函数：验签
def test_signature_roundtrip_is_accepted():
    body = b'{"hello":"world"}'
    assert verify_signature(SECRET, body, sign(SECRET, body)) is True


def test_tampered_body_is_rejected():
    body = b'{"hello":"world"}'
    signature = sign(SECRET, body)
    assert verify_signature(SECRET, b'{"hello":"WORLD"}', signature) is False


def test_wrong_secret_is_rejected():
    body = b'{"hello":"world"}'
    assert verify_signature(SECRET, body, sign("另一个密钥", body)) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "deadbeef",  # 没有 sha256= 前缀
        "sha1=deadbeef",  # 错误的算法前缀
        "sha256=不是ASCII",  # header 是攻击者可控的，非 ASCII 不能把 401 变成 500
        "sha256=" + "f" * 63,  # 长度差一位
    ],
)
def test_malformed_signature_headers_are_rejected(header):
    assert verify_signature(SECRET, b"{}", header) is False


def test_empty_secret_rejects_everything():
    """fail closed：密钥没配置时不是"跳过验签"，而是"全拒"。

    这是本模块最重要的一条断言。默认放行的开关会让接口在某次部署漏配
    环境变量时直接裸奔，而且没有任何报错。
    """
    body = b"{}"
    assert verify_signature("", body, sign("", body)) is False
    assert verify_signature("", body, None) is False


# ====================================================== 纯函数：事件解析
def test_labeled_issue_becomes_a_trigger():
    trigger = extract_issue_trigger(issue_payload(), trigger_label="repopilot")
    assert trigger is not None
    assert trigger.external_ref == "kayou/demo-repo#42"
    assert "divide" in trigger.to_task()


def test_issue_without_the_trigger_label_is_ignored():
    """授权边界：仓库里每个 Issue 都会推事件过来，默认一律不响应。"""
    payload = issue_payload(labels=("bug", "help wanted"))
    assert extract_issue_trigger(payload, trigger_label="repopilot") is None


def test_irrelevant_actions_are_ignored():
    payload = issue_payload(action="closed")
    assert extract_issue_trigger(payload, trigger_label="repopilot") is None


def test_pull_requests_are_not_treated_as_issues():
    """GitHub 把 PR 也当 issue 发事件。不排掉的话我们会去"修"一个 PR。"""
    payload = issue_payload()
    payload["issue"]["pull_request"] = {"url": "https://api.github.com/..."}
    assert extract_issue_trigger(payload, trigger_label="repopilot") is None


# ============================================================== HTTP 层
# 不用模块级 pytestmark：上面那批纯函数测试不该被"Postgres 没起来"拖累。
# 下面每个用例都要 client，而 client fixture 自己依赖 db。


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "github_webhook_secret", SECRET)


async def test_valid_delivery_enqueues_a_run(client):
    response = await post_webhook(client, issue_payload())
    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "queued"
    assert body["external_ref"] == "kayou/demo-repo#42"

    row = await runs_repo.get_run(body["run_id"])
    assert row.status == RunStatus.QUEUED
    assert row.source == "github_issue"
    assert row.external_ref == "kayou/demo-repo#42"


async def test_delivery_is_linked_to_the_run_it_created(client):
    body = (await post_webhook(client, issue_payload())).json()
    delivery = await deliveries_repo.get_delivery("d-1")
    assert str(delivery["run_id"]) == body["run_id"]


async def test_bad_signature_is_401_and_records_nothing(client):
    """验签失败不能留下任何痕迹 —— 先记账再验签等于让任何人往台账里灌垃圾。"""
    response = await post_webhook(client, issue_payload(), tamper=True)
    assert response.status_code == 401
    assert await deliveries_repo.get_delivery("d-1") is None
    assert await runs_repo.list_runs() == []


async def test_missing_signature_header_is_401(client):
    response = await client.post(
        "/webhooks/github",
        content=b"{}",
        headers={"X-GitHub-Delivery": "d-1", "X-GitHub-Event": "issues"},
    )
    assert response.status_code == 401


async def test_missing_delivery_header_is_400(client):
    body = json.dumps(issue_payload()).encode()
    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "issues"},
    )
    assert response.status_code == 400


async def test_replayed_delivery_creates_only_one_run(client):
    """核心场景：GitHub 没在 10 秒内收到 2xx 就会重投，最多 3 次。

    不去重的话，一个 Issue 会开出 3 个 PR。
    """
    first = await post_webhook(client, issue_payload(), delivery="same-id")
    second = await post_webhook(client, issue_payload(), delivery="same-id")

    assert first.status_code == 202
    assert second.json()["status"] == "duplicate"
    assert len(await runs_repo.list_runs()) == 1


async def test_unlabeled_issue_is_ignored_but_still_2xx(client):
    """忽略 ≠ 失败。返回 4xx 会让 GitHub 一直重投一个我们根本不想处理的事件。"""
    response = await post_webhook(client, issue_payload(labels=("bug",)))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert await runs_repo.list_runs() == []


# ================================================ 入队时指向的是目标仓库，不是样例
async def test_the_run_points_at_the_target_repo_not_the_bundled_sample(client, settings):
    """★以前这里写死 `settings.sample_repo` —— 也就是「webhook 收到任何 Issue，
    Agent 都去修我们自己的样例仓库」。现在指向 clone 缓存里 `kayou/demo-repo`
    将来所在的位置。

    注意此刻那个目录**还不存在**：`path_for` 是纯函数，真正的 clone 要等
    worker 领取任务时才做（webhook 的响应超时只有 10 秒）。
    """
    from repopilot.workspace.repos import RepoCache

    body = (await post_webhook(client, issue_payload())).json()
    row = await runs_repo.get_run(body["run_id"])

    assert row.repo_path != str(settings.sample_repo)
    assert row.repo_path == str(RepoCache(settings).path_for("kayou/demo-repo"))
    assert row.repo_path.endswith("/kayou/demo-repo")


async def test_a_repo_outside_the_allowlist_is_ignored(client, monkeypatch):
    """允许名单是第三道纵深（前两道：验签 + Issue 标签）。留空 = 不限。"""
    monkeypatch.setattr(get_settings(), "github_repo_allowlist", ["someone/else"])
    response = await post_webhook(client, issue_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert await runs_repo.list_runs() == []


async def test_a_malformed_repo_name_is_ignored_not_crashed(client):
    """`repo_full_name` 会被拼成路径和 URL。畸形名字要在入队前就挡掉，
    而且是 2xx 忽略 —— 5xx 会让 GitHub 反复重投同一个畸形事件。"""
    payload = issue_payload()
    payload["repository"]["full_name"] = "../../etc/passwd"
    response = await post_webhook(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert await runs_repo.list_runs() == []


async def test_ping_event_is_answered_without_being_recorded(client):
    response = await post_webhook(client, {"zen": "Design for failure."}, event="ping")
    assert response.status_code == 200
    assert response.json()["status"] == "pong"
    assert await deliveries_repo.get_delivery("d-1") is None
