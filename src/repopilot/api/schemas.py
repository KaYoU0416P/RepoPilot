"""HTTP request/response DTOs. Deliberately separate from AgentState."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from repopilot.evaluation import RunEvaluation

RunStatus = Literal["pending", "running", "succeeded", "failed", "error"]


class CreateRunRequest(BaseModel):
    task: str = Field(min_length=3, description="What the agent should change.")
    repo_path: str | None = Field(
        default=None, description="Absolute path to the repo. Defaults to the bundled sample."
    )
    max_retries: int | None = Field(default=None, ge=0, le=5)


class CreateRunResponse(BaseModel):
    run_id: str
    status: RunStatus
    events_url: str


class RunResponse(BaseModel):
    run_id: str
    status: RunStatus
    task: str
    verdict: str | None = None
    final_report: str | None = None
    diff: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    evaluation: RunEvaluation | None = None
    error: str | None = None
    duration_ms: int | None = None


class RunEvent(BaseModel):
    run_id: str
    seq: int
    type: Literal["run_started", "node_completed", "run_finished", "run_error"]
    node: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
