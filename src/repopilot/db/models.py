"""数据库行 → Python 对象。

不用 ORM，所以这里手动做一次映射。好处是你能清楚看到「一行 SQL 结果
长什么样」，坏处是加字段要改两处（schema.sql 和这里）。
MVP 阶段这个代价可以接受。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from repopilot.domain import RunStatus


class RunRow(BaseModel):
    id: UUID
    status: RunStatus
    task: str
    repo_path: str
    source: str
    external_ref: str | None = None

    verdict: str | None = None
    diff: str | None = None
    final_report: str | None = None
    evaluation: dict[str, Any] | None = None
    step_log: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    retry_count: int = 0
    error: str | None = None

    branch: str | None = None
    pr_url: str | None = None

    attempts: int = 0
    max_attempts: int = 3
    locked_by: str | None = None
    lease_expires_at: datetime | None = None

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_record(cls, record: Any) -> "RunRow":
        """asyncpg.Record 长得像 dict，直接喂给 Pydantic 校验。"""
        return cls.model_validate(dict(record))


class ApprovalRow(BaseModel):
    id: int
    run_id: UUID
    decision: str
    decided_by: str
    reason: str | None = None
    decided_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> "ApprovalRow":
        return cls.model_validate(dict(record))
