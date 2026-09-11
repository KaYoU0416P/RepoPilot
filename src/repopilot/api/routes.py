"""HTTP 接口层。

处理函数保持很薄：校验 → 调仓储/服务 → 组装响应。
业务规则在 domain/ 和 db/ 里，不在这里。

**两个 router，不是一个**：`public`（探活 + webhook）和 `router`（其余全部，
默认要 `run` 权限）。分开不是为了好看 —— 鉴权挂在 router 上而不是逐个路由加，
**新写的接口默认就是受保护的**。逐个加注解的失败形态是"忘了加 → 它是公开的"，
而这种漏洞不会有任何报错。要公开就必须显式写进 `public`，一眼数得清。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from repopilot.api.auth import Principal, require_scope
from repopilot.api.schemas import (
    ApprovalRequest,
    CreateRunRequest,
    CreateRunResponse,
    HealthResponse,
    RunEvent,
    RunResponse,
    WebhookResponse,
)
from repopilot.config import Settings, get_settings
from repopilot.db import approvals as approvals_repo
from repopilot.db import deliveries as deliveries_repo
from repopilot.db import runs as runs_repo
from repopilot.db.models import RunRow
from repopilot.domain import ACTIVE, InvalidTransition, RunStatus
from repopilot.github import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    extract_issue_trigger,
    verify_signature,
)
from repopilot.observability import get_logger
from repopilot.worker import DONE, EventBus
from repopilot.workspace.repos import RepoCache, RepoError

log = get_logger(__name__)

#: 不需要 API key 的接口。**只有这两个，改动要慎重。**
#: - `/health`：探活。让负载均衡器先拿一把 key 才能判活，是在给自己挖坑。
#:   所以它**只报够用的信息**，不带版本号、不带连接串、不带队列细节以外的东西。
#: - `/webhooks/github`：它有**自己的**认证（HMAC 验签），而且 GitHub 那边
#:   没法配 Authorization 头 —— 给它加 API key 只会把真正的调用方挡在外面。
public = APIRouter()

#: 其余全部。默认要 `run` 权限 —— **新加的路由自动受保护**。
router = APIRouter(dependencies=[Depends(require_scope("run"))])


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus


# ------------------------------------------------------------------ health
@public.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    try:
        depth = await runs_repo.queue_depth()
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        depth, database = {}, f"error: {type(exc).__name__}"
    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        llm_provider=settings.llm_provider,
        model=settings.model,
        database=database,
        worker_enabled=settings.enable_worker,
        queue=depth,
    )


# -------------------------------------------------------------------- runs
@router.post("/runs", response_model=CreateRunResponse, status_code=202)
async def create_run(
    body: CreateRunRequest,
    settings: Settings = Depends(get_settings),
) -> CreateRunResponse:
    """只入队，不执行。立刻返回 202，由 worker 异步领取。"""
    repo_path = _resolve_repo_path(body.repo_path, settings)
    row = await runs_repo.create_run(
        task=body.task,
        repo_path=str(repo_path),
        source="manual",
        max_attempts=body.max_attempts or settings.max_attempts,
    )
    return CreateRunResponse(
        run_id=row.id,
        status=row.status,
        events_url=f"/runs/{row.id}/events",
    )


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(
    status: RunStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RunResponse]:
    rows = await runs_repo.list_runs(status=status, limit=min(limit, 200), offset=offset)
    return [RunResponse.from_row(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID) -> RunResponse:
    return RunResponse.from_row(await _require(run_id))


@router.post("/runs/{run_id}/cancel", response_model=RunResponse, status_code=202)
async def cancel_run(run_id: UUID) -> RunResponse:
    await _require(run_id)
    try:
        row = await runs_repo.transition(run_id, RunStatus.CANCELLED)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_row(row)


# ---------------------------------------------------------------- approval
@router.post(
    "/runs/{run_id}/approval",
    response_model=RunResponse,
    # ★这一个接口要**单独的**权限位，不是 router 默认的 `run`。
    # 整个项目的核心论点是"Agent 自己说成功不算数，要人批准"。
    # 如果开 run 的那把 key 也能批准自己开的 run，**这道闸门就是装饰品**。
    # CI 机器人拿 `run`，人拿 `run,approve` —— 权限模型要长得像业务约束。
    dependencies=[Depends(require_scope("approve"))],
)
async def decide_approval(
    run_id: UUID,
    body: ApprovalRequest,
    principal: Principal = Depends(require_scope("approve")),
) -> RunResponse:
    """人工审批闸门。只有 pending_approval 的 run 能被批准/驳回。"""
    await _require(run_id)
    try:
        await approvals_repo.decide(
            run_id,
            decision=body.decision,
            # ★`decided_by` 取**认证出来的身份**，不取请求体。
            #
            # 以前它是 `body.decided_by` —— 也就是说审批流水上那句"谁批的"
            # 是**被审计的人自己填的**，随手写 "CTO" 就行。那不是审计，是留言板。
            # 一个只有人能看的字段，和一个能作为证据的字段，差别就在这一行。
            #
            # 通用结论：**审计字段绝不能由被审计者提供。** 谁、什么时候，
            # 都得由服务端从已验证的上下文里取。
            decided_by=principal.name,
            reason=body.reason,
        )
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_row(await _require(run_id))


@router.get("/runs/{run_id}/approvals")
async def approval_history(run_id: UUID) -> list[dict]:
    await _require(run_id)
    return [a.model_dump(mode="json") for a in await approvals_repo.history(run_id)]


# ----------------------------------------------------------------- webhook
@public.post("/webhooks/github", response_model=WebhookResponse)
async def github_webhook(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> WebhookResponse:
    """GitHub 事件入口。

    顺序是有讲究的，每一步都不能提前也不能挪后：

      1. 读 **raw bytes**    验签必须对原文做，反序列化过就废了
      2. 验签                 没通过 → 401，且**什么都不记**。
                              先记账再验签 = 任何人都能往你的台账里灌垃圾
      3. 幂等登记             重投 → 直接 200，不重复干活
      4. 解析 + 授权判断      没有 repopilot 标签 → 忽略（仍然 2xx）
      5. 入队                 走已有的 create_run，后面一整条链路不用改

    第 3 步之后失败会有个已知缺口：投递已登记但 run 没建成，重投也会被判重，
    事件就丢了。工业级做法是把"登记 + 入队"放进同一个事务
    （两张表在同一个库里，做得到）。这里没做，是刻意留的取舍 —— 见 progress.md。
    """
    body = await request.body()

    # ---- 1 & 2：验签。fail closed —— 密钥没配就全拒。
    if not verify_signature(
        settings.github_webhook_secret, body, request.headers.get(SIGNATURE_HEADER)
    ):
        raise HTTPException(status_code=401, detail="签名校验失败")

    delivery_id = request.headers.get(DELIVERY_HEADER)
    if not delivery_id:
        raise HTTPException(status_code=400, detail=f"缺少 {DELIVERY_HEADER}")

    event_type = request.headers.get(EVENT_HEADER, "")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体不是 JSON 对象")

    # GitHub 配置 webhook 时先发一个 ping 探活，别把它记进台账。
    if event_type == "ping":
        return WebhookResponse(status="pong", detail="webhook 已连通")

    # ---- 3：幂等。判重和登记是同一条 INSERT，天然原子。
    is_new = await deliveries_repo.claim_delivery(
        delivery_id, source="github", event_type=event_type, payload=payload
    )
    if not is_new:
        return WebhookResponse(status="duplicate", detail=f"投递 {delivery_id} 已处理过")

    # ---- 4：授权判断。默认不响应，打了标签才算授权。
    if event_type != "issues":
        return WebhookResponse(status="ignored", detail=f"不处理的事件类型: {event_type}")

    trigger = extract_issue_trigger(payload, trigger_label=settings.github_trigger_label)
    if trigger is None:
        return WebhookResponse(
            status="ignored", detail=f"未打 {settings.github_trigger_label} 标签或动作无关"
        )

    # ---- 4.5：允许名单。第三道纵深（前两道是验签和标签），留空 = 不限。
    allowlist = settings.github_repo_allowlist
    if allowlist and trigger.repo_full_name not in allowlist:
        return WebhookResponse(
            status="ignored", detail=f"仓库不在允许名单里: {trigger.repo_full_name}"
        )

    # ---- 5：入队。汇入和 POST /runs 完全相同的下游链路。
    #
    # ★`repo_path` 写的是**缓存里将来那个位置**，此刻它还不存在 ——
    # clone 要等 worker 领取任务时才做。webhook 处理函数里绝不能 clone：
    # GitHub 的响应超时是 10 秒，clone 一个真实仓库远不止，超时它会判失败并
    # 重投，于是变成「每次都超时 → 每次都重投 → 每次都重新 clone」。
    # `path_for` 是纯函数，入队时和执行时算出来的是同一个路径。
    try:
        repo_path = RepoCache(settings).path_for(trigger.repo_full_name)
    except RepoError as exc:
        log.warning("仓库名不合法，忽略: %s", exc)
        return WebhookResponse(status="ignored", detail=str(exc))

    row = await runs_repo.create_run(
        task=trigger.to_task(),
        repo_path=str(repo_path),
        source="github_issue",
        external_ref=trigger.external_ref,
        max_attempts=settings.max_attempts,
    )
    await deliveries_repo.attach_run(delivery_id, row.id)
    log.info("webhook 入队: %s -> run %s", trigger.external_ref, row.id)

    response.status_code = 202
    return WebhookResponse(
        status="queued", run_id=row.id, external_ref=trigger.external_ref
    )


# --------------------------------------------------------------------- SSE
@router.get("/runs/{run_id}/events")
async def stream_events(run_id: UUID, bus: EventBus = Depends(get_bus)):
    """Server-Sent Events：一个长连接，每个节点完成推一帧 `data: <json>`。"""
    row = await _require(run_id)

    # 已经是终态：没有后续事件了，回放一次就关，别让客户端干等
    if row.status not in ACTIVE:
        queue = bus.subscribe(run_id, replay=True)
        queue.put_nowait(DONE)
    else:
        queue = bus.subscribe(run_id, replay=True)

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if not isinstance(item, RunEvent):  # 哨兵
                    yield "event: done\ndata: {}\n\n"
                    return
                yield f"event: {item.type}\ndata: {item.model_dump_json()}\n\n"
        except asyncio.CancelledError:
            raise  # 客户端断开
        finally:
            bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ 内部
def _resolve_repo_path(raw: str | None, settings: Settings) -> Path:
    """同步函数：两次 stat 调用，别放在 async 处理函数里。"""
    path = Path(raw).expanduser().resolve() if raw else settings.sample_repo
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"仓库路径不存在: {path}")
    return path


async def _require(run_id: UUID) -> RunRow:
    row = await runs_repo.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    return row
