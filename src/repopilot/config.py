"""Central settings. Reads env vars prefixed with REPOPILOT_ (plus ANTHROPIC_API_KEY)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOPILOT_",
        env_file=".env",
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: Literal["anthropic", "scripted"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = ""
    max_tokens: int = 4096

    # --- Agent budget ---
    max_retries: int = 2
    max_files_per_edit: int = 5

    # --- Safety limits ---
    tool_timeout_seconds: float = 20.0
    test_timeout_seconds: float = 60.0
    max_concurrent_tools: int = 4
    max_file_bytes: int = 200_000

    # --- Workspace ---
    workspace_root: Path = PROJECT_ROOT / ".workspaces"
    sample_repo: Path = PROJECT_ROOT / "fixtures" / "sample_repo"


@lru_cache
def get_settings() -> Settings:
    """Cached so the whole process shares one Settings instance."""
    import os

    s = Settings()
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if s.llm_provider == "anthropic" and not s.anthropic_api_key:
        s.llm_provider = "scripted"
    return s
