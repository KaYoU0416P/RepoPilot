"""Git tools. Run through the sandbox so they inherit the timeout policy."""

from repopilot.config import get_settings
from repopilot.sandbox import run_command
from repopilot.tools.base import ToolResult, tool
from repopilot.workspace import Workspace


@tool(
    name="git_diff",
    description="Unified diff of all agent changes against the baseline commit.",
    risk="read",
    parameters={},
)
async def git_diff(workspace: Workspace) -> ToolResult:
    settings = get_settings()
    # `git add -A` first so newly created files show up in the diff too.
    await run_command(
        ["git", "add", "-A"], cwd=workspace.root, timeout=settings.tool_timeout_seconds
    )
    result = await run_command(
        ["git", "diff", "--cached", "--stat", "--patch"],
        cwd=workspace.root,
        timeout=settings.tool_timeout_seconds,
    )
    if not result.ok:
        return ToolResult(tool="git_diff", ok=False, error=result.stderr or "git diff failed")
    return ToolResult(
        tool="git_diff",
        ok=True,
        content=result.stdout or "(no changes)",
        meta={"empty": not result.stdout.strip()},
    )
