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
    # ---- 第 1 步：编译正则。非法正则不能让整个 run 崩掉，转成 ok=False ----
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(
            tool="search_code",
            ok=False,
            error=f"invalid regex {pattern!r}: {exc}",
        )

    # ---- 第 2 步：准备一个空列表装结果 ----
    matches: list[str] = []

    # ---- 第 3 步：外层循环，遍历每个文件 ----
    for path in workspace.iter_files(glob):
        if len(matches) >= max_results:
            break

        # 读不出来的文件（二进制、没权限）跳过，不要让一个坏文件毁掉整次搜索
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        rel = workspace.relative(path)

        # ---- 第 4 步：内层循环，遍历每一行 ----
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{rel}:{lineno}: {line.strip()}")
                if len(matches) >= max_results:
                    break

    # ---- 第 5 步：组装返回。注意搜不到也是 ok=True ----
    return ToolResult(
        tool="search_code",
        ok=True,
        content="\n".join(matches) if matches else "(no matches)",
        meta={"count": len(matches)},
    )
