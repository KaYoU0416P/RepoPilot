import os

import pytest

from repopilot.config import get_settings
from repopilot.tools import build_registry
from repopilot.workspace import WorkspaceManager

# Tests must never hit the network or spend tokens.
os.environ["REPOPILOT_LLM_PROVIDER"] = "scripted"
get_settings.cache_clear()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def workspace(settings, tmp_path):
    """A throwaway copy of fixtures/sample_repo, git-initialised, deleted after."""
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create(settings.sample_repo, run_id="test")
    yield ws
    manager.cleanup(ws)


@pytest.fixture
def registry():
    return build_registry()
