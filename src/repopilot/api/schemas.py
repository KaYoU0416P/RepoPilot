"""HTTP 请求/响应 DTO。和 AgentState、和数据库行都刻意分开。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from repopilot.db.models import RunRow
from repopilot.domain import RunStatus


class CreateRunRequest(BaseModel):
    task: str = Field(min_length=3, description="要 Agent 做的修改")
    repo_path: str | None = Field(default=None, description="仓库绝对路径，不填用内置样例")
    max_attempts: int | None = Field(default=None, ge=1, le=5)


class CreateRunResponse(BaseModel):
    run_id: UUID
    status: RunStatus
    events_url: str
    deduplicated: bool = Field(default=False, description="命中幂等，复用了已有的 run")


class ApprovalRequest(BaseModel):
    """★注意这里**没有** `decided_by`。

    它以前有，也就是说审批流水上"谁批的"是被审计的人自己填的 ——
    随手写个 "CTO" 就行。现在它取自认证出来的身份（`routes.py`）。
    **审计字段绝不能由被审计者提供。**
    """

    decision: Literal["approved", "rejected"]
    reason: str | None = None


class RunResponse(BaseModel):
    run_id: UUID
    status: RunStatus
    task: str
    source: str
    external_ref: str | None = None

    verdict: str | None = None
    final_report: str | None = None
    diff: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    evaluation: dict[str, Any] | None = None
    error: str | None = None

    #: 发布产物。status 是 published 但 pr_url 为空 = 走的空转发布器，
    #: 没有真的 PR。别让人误以为 PR 开好了。
    branch: str | None = None
    pr_url: str | None = None

    attempts: int = 0
    retry_count: int = 0
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_row(cls, row: RunRow) -> "RunResponse":
        return cls(
            run_id=row.id,
            status=row.status,
            task=row.task,
            source=row.source,
            external_ref=row.external_ref,
            verdict=row.verdict,
            final_report=row.final_report,
            diff=row.diff,
            files_changed=row.files_changed,
            steps=row.step_log,
            evaluation=row.evaluation,
            error=row.error,
            branch=row.branch,
            pr_url=row.pr_url,
            attempts=row.attempts,
            retry_count=row.retry_count,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )


class WebhookResponse(BaseModel):
    """webhook 的响应体。

    注意所有分支都是 2xx —— 只要签名对、投递被受理，就算"我收到了"。
    对 GitHub 返回 4xx/5xx 会触发重投，而"这个 Issue 没有 repopilot 标签"
    并不是一个需要重投的错误。
    """

    status: Literal["queued", "duplicate", "ignored", "pong"]
    detail: str = ""
    run_id: UUID | None = None
    external_ref: str | None = None


class RunEvent(BaseModel):
    run_id: str
    seq: int
    type: Literal["run_started", "node_completed", "run_finished", "run_error"]
    node: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    llm_provider: str
    model: str
    database: str
    worker_enabled: bool
    queue: dict[str, int] = Field(default_factory=dict)
