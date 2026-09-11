"""API 鉴权的契约。

这个模块要证明的三件事，按重要性排序：

1. **默认拒绝** —— 每一个业务接口都要 key，不是"大部分要"。
   所以这里不是挑几个接口测，而是**把路由表整个遍历一遍**。
2. **审批要单独的权限位** —— 否则审批闸门是装饰品。
3. **`decided_by` 来自身份不来自请求体** —— 否则审批流水是留言板。
"""

import httpx
import pytest

from conftest import APPROVER_KEY, CI_KEY
from repopilot.api.auth import Principal, identify
from repopilot.config import ApiKeyConfig, Settings
from repopilot.db import approvals as approvals_repo
from repopilot.db import runs as runs_repo
from repopilot.domain import RunStatus

#: 不需要 key 的接口。**清单写死在测试里**，这样任何人把一个接口挪出鉴权
#: 都会让测试变红 —— 公开接口的数量必须是一个需要**显式修改**才能变的东西。
PUBLIC_PATHS = {"/health", "/webhooks/github"}


def _client(app, key: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    )


# ==================================================== 1. 默认拒绝（遍历路由表）
def _business_routes(app):
    """从 app 的路由表里捞出所有该受保护的接口。

    ★不手写清单：手写的清单会跟着新接口一起过期，而**过期的安全测试
    比没有测试更糟** —— 它给你一个"测过了"的错觉。

    走 `app.openapi()` 而不是 `app.routes`：后者在不同 FastAPI 版本里形状
    不一样（新版把 `include_router` 进来的东西包成 `_IncludedRouter`，
    不摊平），一递归就要开始猜私有属性。**OpenAPI 文档是公开契约，
    它列出来的就是对外真实存在的接口** —— 正是这个测试想遍历的集合。
    """
    for path, operations in app.openapi()["paths"].items():
        if path in PUBLIC_PATHS:
            continue
        for method in operations:
            yield method.upper(), path


async def test_every_business_route_rejects_an_anonymous_call(app_under_test):
    """★核心断言：**一个都不能漏。**

    鉴权挂在 `APIRouter(dependencies=...)` 上而不是逐个路由加，就是为了这个 ——
    逐个加的失败形态是"新写了个接口忘了加 → 它是公开的"，而且不会有任何报错。
    """
    checked = 0
    async with _client(app_under_test, None) as c:
        for method, path in _business_routes(app_under_test):
            url = path.replace("{run_id}", "00000000-0000-0000-0000-000000000000")
            response = await c.request(method, url, json={})
            assert response.status_code == 401, f"{method} {path} 没有鉴权！"
            # 少了这个头就不是合规的 401（RFC 9110）：客户端靠它知道该用哪种认证重试
            assert response.headers.get("WWW-Authenticate") == "Bearer"
            checked += 1
    assert checked >= 6, f"只检查到 {checked} 个路由，路由表是不是没捞全"


async def test_health_and_webhook_stay_public(app_under_test):
    """反过来验一条：公开的必须还公开。

    `/health` 让负载均衡器先拿一把 key 才能判活，是在给自己挖坑；
    `/webhooks/github` 有**自己的** HMAC 验签，而 GitHub 那边配不了
    Authorization 头 —— 给它加 API key 只会把真正的调用方挡在外面。
    """
    async with _client(app_under_test, None) as c:
        assert (await c.get("/health")).status_code == 200
        # 没签名 → 401，但那是**验签**给的 401，不是 API key 给的
        webhook = await c.post("/webhooks/github", content=b"{}")
        assert webhook.status_code == 401
        assert "签名" in webhook.json()["detail"]


@pytest.mark.parametrize(
    "header",
    [
        {},  # 没有 header
        {"Authorization": ""},
        {"Authorization": "test-key-approver-00000000"},  # 少了 scheme
        {"Authorization": "Basic test-key-approver-00000000"},  # 错的 scheme
        {"Authorization": "Bearer "},  # 空 token
        {"Authorization": "Bearer wrong-key-0000000000000"},
        {"Authorization": "Bearer test-key-approver-0000000"},  # 少一位
        {"Authorization": "Bearer test-key-approver-000000000"},  # 多一位
    ],
)
async def test_malformed_or_wrong_credentials_are_401(app_under_test, header):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test),
        base_url="http://test",
        headers=header,
    ) as c:
        assert (await c.get("/runs")).status_code == 401


async def test_the_401_body_does_not_say_why(app_under_test, monkeypatch):
    """★"服务端没配 key" 和 "你的 key 不对" 对外必须是**同一个** 401。

    不然 401 本身就成了一个探测接口（"哦，这台服务器压根没配 key"）。
    详细原因只进日志。**报错要对运维详细、对外部统一。**
    """
    from repopilot.config import get_settings

    async with _client(app_under_test, "wrong-key-0000000000000") as c:
        wrong_key = await c.get("/runs")

    monkeypatch.setattr(get_settings(), "api_keys", [])
    async with _client(app_under_test, APPROVER_KEY) as c:
        no_keys = await c.get("/runs")

    assert wrong_key.status_code == no_keys.status_code == 401
    assert wrong_key.json() == no_keys.json()


async def test_no_keys_configured_denies_everything(app_under_test, monkeypatch):
    """★fail closed：一把 key 都没配 = 拒绝所有，不是"没配就不鉴权"。

    和 `github_webhook_secret` 同一条规矩。默认放行的开关是最典型的生产事故：
    某次部署漏注入一个环境变量，接口就裸奔了，日志里一条报错都没有。
    """
    from repopilot.config import get_settings

    monkeypatch.setattr(get_settings(), "api_keys", [])
    async with _client(app_under_test, APPROVER_KEY) as c:
        assert (await c.get("/runs")).status_code == 401
        assert (await c.get("/health")).status_code == 200, "探活不该被连坐"


# ============================================ 2. 审批要单独的权限位（不是 401）
async def test_a_run_scoped_key_can_do_run_things(app_under_test):
    async with _client(app_under_test, CI_KEY) as c:
        assert (await c.get("/runs")).status_code == 200
        created = await c.post("/runs", json={"task": "修个 bug"})
        assert created.status_code == 202


async def test_a_run_scoped_key_cannot_approve(app_under_test, db):
    """★整个项目的核心论点是「Agent 说成功不算数，要人批准」。

    如果开 run 的那把 key 也能批准自己开的 run，**这道闸门就是装饰品**。
    CI 机器人拿 `run`，人拿 `run,approve`。**权限模型要长得像业务约束。**
    """
    row = await runs_repo.create_run(task="t", repo_path="/tmp/x")
    await runs_repo.transition(row.id, RunStatus.RUNNING)
    await runs_repo.transition(row.id, RunStatus.PENDING_APPROVAL)

    async with _client(app_under_test, CI_KEY) as c:
        denied = await c.post(f"/runs/{row.id}/approval", json={"decision": "approved"})
    # ★403 不是 401：身份是认的，权限不够。回 401 会让调用方以为 key 坏了，
    # 跑去重新签发一把同样没权限的。
    assert denied.status_code == 403
    assert "approve" in denied.json()["detail"]

    assert (await runs_repo.get_run(row.id)).status == RunStatus.PENDING_APPROVAL
    assert await approvals_repo.history(row.id) == [], "被拒的请求不该留下审批流水"

    async with _client(app_under_test, APPROVER_KEY) as c:
        allowed = await c.post(f"/runs/{row.id}/approval", json={"decision": "approved"})
    assert allowed.status_code == 200


# ================================ 3. decided_by 来自身份，不来自请求体
async def test_the_approver_cannot_forge_who_approved(app_under_test, db):
    """★以前 `decided_by` 是请求体里的字段 —— 审批流水上"谁批的"是
    **被审计的人自己填的**，随手写 "the CTO" 就行。那不是审计，是留言板。

    **审计字段绝不能由被审计者提供。**
    """
    row = await runs_repo.create_run(task="t", repo_path="/tmp/x")
    await runs_repo.transition(row.id, RunStatus.RUNNING)
    await runs_repo.transition(row.id, RunStatus.PENDING_APPROVAL)

    async with _client(app_under_test, APPROVER_KEY) as c:
        response = await c.post(
            f"/runs/{row.id}/approval",
            # 伪造尝试：请求体里自称是 CTO
            json={"decision": "approved", "decided_by": "the CTO", "reason": "我说的算"},
        )
    assert response.status_code == 200

    history = await approvals_repo.history(row.id)
    assert len(history) == 1
    assert history[0].decided_by == "kayou", "审批流水信了请求体里的身份"
    assert history[0].reason == "我说的算", "reason 是主观说明，本来就该由人填"


# ======================================================= 比较必须是常数时间
def test_identify_uses_a_constant_time_comparison():
    """`==` 一发现某字节不同就返回，耗时泄露了"前几位对上了"，攻击者
    逐字节就能把 key 试出来。和 webhook 验签同一个坑。

    时序本身测不了（会飘），但能钉住"用的是 `hmac.compare_digest`"这个事实。
    """
    import inspect

    source = inspect.getsource(identify)
    # 把文档字符串摘掉再看 —— 它里面恰好在讲"别用 =="，会自己命中自己。
    code = source.replace(identify.__doc__ or "", "")
    assert "compare_digest" in code
    assert "==" not in code.replace("!=", ""), "别用 == 比密钥"


def test_identify_maps_a_key_to_its_principal():
    settings = Settings(
        api_keys=[
            ApiKeyConfig(key="k" * 20, name="ci", scopes=["run"]),
            ApiKeyConfig(key="j" * 20, name="human", scopes=["run", "approve"]),
        ]
    )
    assert identify("k" * 20, settings) == Principal("ci", frozenset({"run"}))
    assert identify("j" * 20, settings).can("approve")
    assert identify("k" * 20, settings).can("approve") is False
    assert identify("nope", settings) is None


def test_a_key_must_be_long_enough_to_be_a_secret():
    """16 字符下限挡的是 `key="test"` 这种 —— 配置项的校验就该在配置层做，
    别指望调用方自觉。"""
    with pytest.raises(ValueError):
        ApiKeyConfig(key="short", name="oops")
