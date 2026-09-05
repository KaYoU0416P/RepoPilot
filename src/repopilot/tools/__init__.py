"""Tool package. `build_registry()` is the single place tools become callable."""

from repopilot.config import get_settings
from repopilot.tools.base import Risk, ToolRegistry, ToolResult, ToolSpec, tool
from repopilot.tools.fs_tools import list_files, read_file, search_code, write_file
from repopilot.tools.git_tools import git_diff
from repopilot.tools.test_runner import run_tests

ALL_TOOLS = [list_files, read_file, search_code, write_file, git_diff, run_tests]


def build_registry() -> ToolRegistry:
    settings = get_settings()
    registry = ToolRegistry(
        timeout=settings.tool_timeout_seconds,
        max_concurrency=settings.max_concurrent_tools,
    )
    for fn in ALL_TOOLS:
        registry.register(fn.__tool_spec__)
    return registry


__all__ = [
    "ALL_TOOLS",
    "Risk",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_registry",
    "git_diff",
    "list_files",
    "read_file",
    "run_tests",
    "search_code",
    "tool",
    "write_file",
]
