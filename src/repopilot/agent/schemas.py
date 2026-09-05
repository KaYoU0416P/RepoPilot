"""Pydantic models the LLM must fill in. These double as the JSON schemas we send."""

from pydantic import BaseModel, Field


class Analysis(BaseModel):
    """Initial read of the task against the repository tree."""

    reasoning: str = Field(description="Short explanation of what the task requires.")
    relevant_files: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths worth reading, most relevant first. Max 5.",
    )
    search_queries: list[str] = Field(
        default_factory=list,
        description="Regex patterns to locate the relevant code. Max 3.",
    )


class Plan(BaseModel):
    """High level strategy, decided once, reused across retries."""

    summary: str = Field(description="One sentence: what change will be made.")
    approach: str = Field(description="How the change will be made, 2-4 sentences.")
    files_to_edit: list[str] = Field(
        default_factory=list, description="Repo-relative paths that will be modified."
    )


class FileEdit(BaseModel):
    """A whole-file replacement. Simpler and far more robust than patch formats."""

    path: str = Field(description="Repo-relative path to write.")
    content: str = Field(description="Complete new content of the file.")
    rationale: str = Field(default="", description="Why this edit.")


class EditSet(BaseModel):
    """The concrete edits for one attempt."""

    rationale: str = Field(default="", description="Overall reasoning for this attempt.")
    edits: list[FileEdit] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
    """Flattened tool telemetry, consumed by evaluation and the API response."""

    tool: str
    ok: bool
    duration_ms: int
    args_preview: str = ""
    error: str | None = None


class TestOutcome(BaseModel):
    __test__ = False  # stop pytest from trying to collect this as a test class

    passed: bool
    exit_code: int | None = None
    timed_out: bool = False
    summary: str = ""
    output: str = ""
