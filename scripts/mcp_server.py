"""RepoPilot MCP server 的 stdio 入口。

    uv run python scripts/mcp_server.py --repo fixtures/sample_repo
    uv run python scripts/mcp_server.py --repo /path/to/repo --allow-write

接进 Claude Desktop / Claude Code：

    {
      "mcpServers": {
        "repopilot": {
          "command": "uv",
          "args": ["run", "--directory", "/Users/kayou/Documents/py_agent",
                   "python", "scripts/mcp_server.py",
                   "--repo", "/path/to/repo"]
        }
      }
    }

**工具跑在仓库的一次性副本上，不是原仓库。** 所以即使开了 --allow-write，
对端也改不到你的真实代码；改动留在 `.workspaces/mcp-*` 里，用 git_diff 看。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from repopilot.config import get_settings
from repopilot.mcp import MCPServer
from repopilot.observability import setup_logging
from repopilot.tools import build_registry
from repopilot.workspace import WorkspaceManager


async def stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """把进程的 stdin/stdout 包装成 asyncio 的流。

    不能直接用 `sys.stdin.readline()` —— 那是阻塞调用，会把整个事件循环冻住，
    包括正在跑的工具。必须通过 `connect_read_pipe` 交给事件循环去 poll。
    （和 sandbox 里坚持用 create_subprocess_exec 是同一条纪律。）
    """
    loop = asyncio.get_running_loop()

    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def main() -> int:
    parser = argparse.ArgumentParser(description="RepoPilot MCP server (stdio)")
    parser.add_argument("--repo", type=Path, help="要暴露的仓库，默认用内置样例")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="同时暴露 write_file / run_tests。默认只暴露只读工具。",
    )
    args = parser.parse_args()

    # ★日志必须走 stderr：stdout 是 JSON-RPC 的协议通道，
    # 往里写一行日志就等于发了一条畸形报文，连接直接废掉。
    setup_logging(stream=sys.stderr)

    settings = get_settings()
    repo = (args.repo or settings.sample_repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"仓库不存在: {repo}", file=sys.stderr)
        return 1

    # 在副本上干活，不碰原仓库。和 Agent 走的是同一套 workspace 机制。
    workspace = WorkspaceManager(settings.workspace_root).create(repo, run_id="mcp")
    server = MCPServer(build_registry(), workspace, allow_write=args.allow_write)

    reader, writer = await stdio_streams()
    await server.serve(reader, writer)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
