"""The one 'execute' tool: run the target repo's test suite inside the sandbox."""

import re
import sys

from repopilot.config import get_settings
from repopilot.sandbox import run_command
from repopilot.tools.base import ToolResult, tool
from repopilot.workspace import Workspace

_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors)")


@tool(
    name="run_tests",
    description="Run pytest in the workspace. Returns pass/fail plus captured output.",
    risk="execute",
    parameters={"target": "str, optional pytest path/expression"},
)
async def run_tests(workspace: Workspace, target: str = "") -> ToolResult:
    settings = get_settings()
    command = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    if target:
        command.append(workspace.relative(workspace.resolve(target)))

    result = await run_command(
        command,
        cwd=workspace.root,
        timeout=settings.test_timeout_seconds,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    output = f"{result.stdout}\n{result.stderr}".strip()

    if result.timed_out:
        return ToolResult(
            tool="run_tests",
            ok=False,
            error=f"tests timed out after {settings.test_timeout_seconds}s",
            content=output,
            meta={"timed_out": True, "passed": False},
        )

    counts = {kind: int(n) for n, kind in _SUMMARY.findall(output)}
    return ToolResult(
        tool="run_tests",
        ok=True,  # the tool ran; whether tests passed is in meta
        content=output,
        meta={
            "passed": result.exit_code == 0,
            "exit_code": result.exit_code,
            "counts": counts,
            "duration_ms": result.duration_ms,
        },
    )
