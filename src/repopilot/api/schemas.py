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
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1)
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
            attempts=row.attempts,
            retry_count=row.retry_count,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )


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
