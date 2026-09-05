"""Filesystem tools. All paths go through Workspace.resolve()."""

import re

from repopilot.config import get_settings
from repopilot.tools.base import ToolResult, tool
from repopilot.workspace import Workspace


@tool(
    name="list_files",
    description="List files in the repository. Optional glob, e.g. '**/*.py'.",
    risk="read",
    parameters={"glob": "str, default '**/*'"},
)
async def list_files(workspace: Workspace, glob: str = "**/*") -> ToolResult:
    paths = [workspace.relative(p) for p in workspace.iter_files(glob)]
    return ToolResult(
        tool="list_files",
        ok=True,
        content="\n".join(paths) or "(empty)",
        meta={"count": len(paths)},
    )


@tool(
    name="read_file",
    description="Read a text file from the repository, relative to repo root.",
    risk="read",
    parameters={"path": "str, repo-relative"},
)
async def read_file(workspace: Workspace, path: str) -> ToolResult:
    target = workspace.resolve(path)
    if not target.is_file():
        return ToolResult(tool="read_file", ok=False, error=f"not a file: {path}")

    max_bytes = get_settings().max_file_bytes
    if target.stat().st_size > max_bytes:
        return ToolResult(tool="read_file", ok=False, error=f"file too large (> {max_bytes} bytes)")

    text = target.read_text(encoding="utf-8", errors="replace")
    numbered = "\n".join(f"{i:>4} | {line}" for i, line in enumerate(text.splitlines(), start=1))
    return ToolResult(
        tool="read_file",
        ok=True,
        content=numbered,
        meta={"path": path, "lines": text.count("\n") + 1},
    )


@tool(
    name="write_file",
    description="Overwrite a file with new content. Creates parent directories.",
    risk="write",
    parameters={"path": "str, repo-relative", "content": "str, full new file content"},
)
async def write_file(workspace: Workspace, path: str, content: str) -> ToolResult:
    target = workspace.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(content, encoding="utf-8")
    return ToolResult(
        tool="write_file",
        ok=True,
        content=f"{'updated' if existed else 'created'} {path} ({len(content)} bytes)",
        meta={"path": path, "created": not existed},
    )


@tool(
    name="search_code",
    description="Regex search across repository files. Returns 'path:line: text' matches.",
    risk="read",
    parameters={
        "pattern": "str, Python regex",
        "glob": "str, default '**/*.py'",
        "max_results": "int, default 50",
    },
)
async def search_code(
    workspace: Workspace,
    pattern: str,
    glob: str = "**/*.py",
    max_results: int = 50,
) -> ToolResult:
    """TODO(you): implement this. See tests/test_search_code.py for the contract.

    Requirements:
      1. Compile `pattern` as a regex. On re.error -> ToolResult(ok=False, error=...).
      2. For every file in workspace.iter_files(glob), scan line by line.
      3. Collect matches formatted as f"{rel_path}:{lineno}: {line.strip()}".
      4. Stop once len(matches) == max_results.
      5. Return ok=True, content="\\n".join(matches), meta={"count": len(matches)}.
         Zero matches is still ok=True with content "(no matches)".
      6. Skip files that fail to decode as UTF-8 rather than crashing the run.

    Useful: `re.compile`, `workspace.iter_files(glob)`, `workspace.relative(path)`,
    `path.read_text(encoding="utf-8", errors="replace")`, `str.splitlines()`.
    """
    raise NotImplementedError("search_code is your handwrite task - see docstring")


_ = re  # kept so the import is available for your implementation
