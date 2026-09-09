"""MCP server：把 RepoPilot 的 6 个工具通过 stdio 暴露给任何 MCP 客户端。

**服务端是自己实现的**（`protocol.py` 手写 JSON-RPC 2.0），不是接别人的 SDK。

## 为什么这层这么薄

工具本来就在 `ToolRegistry` 后面，超时、并发上限、异常降级都是注册表统一加的。
所以 MCP 层只做三件事：**协议解析、Schema 转换、结果包装**。
一行业务逻辑都没有 —— 这正是当初把横切关注点收敛到注册表里的回报。

## 两个必须说对的设计

**1. stdout 是协议通道，日志必须走 stderr。**
   stdio 传输下 stdin/stdout 就是那根管子。往 stdout print 一行日志，
   对端收到的就是一条畸形报文。所以入口调的是
   `setup_logging(stream=sys.stderr)`。这是 MCP stdio server 最经典的坑。

**2. 工具失败 ≠ RPC 失败。**
   `read_file` 读了个不存在的文件，这是**正常的业务结果**，要以
   `result: {isError: true, content:[...]}` 返回，让模型看得到错误、能改了重试。
   只有「方法名不认识」「参数结构不对」这种**协议层**问题才回 JSON-RPC error。
   混淆这两层，模型就永远看不到工具的报错。

## 安全默认值

默认**只暴露 risk="read" 的工具**。`write_file` 和 `run_tests` 要显式
`--allow-write` 才开。理由：MCP 客户端是外部的、我们不控制的，而
`run_tests` 是会执行仓库里代码的。整个项目的主线就是「不信任模型输出」，
这里没有理由破例。

workspace 由服务端启动时钉死，**客户端无法指定路径** —— 否则
`Workspace.resolve()` 的路径收敛就被整个绕过去了。
"""

import asyncio
from typing import Any

from repopilot.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JsonRpcError,
    is_notification,
    make_error,
    make_result,
    parse_message,
)
from repopilot.mcp.schema import describe_tool
from repopilot.observability import get_logger
from repopilot.tools import ToolRegistry
from repopilot.workspace import Workspace

log = get_logger(__name__)

SERVER_NAME = "repopilot"
SERVER_VERSION = "0.3.0"
#: 我们实现的 MCP 版本。握手时原样告诉客户端，让它自己决定能不能聊。
PROTOCOL_VERSION = "2025-06-18"


class MCPServer:
    def __init__(
        self,
        registry: ToolRegistry,
        workspace: Workspace,
        *,
        allow_write: bool = False,
    ) -> None:
        self.registry = registry
        self.workspace = workspace
        self.allow_write = allow_write
        self._initialized = False

    # ------------------------------------------------------------ 暴露哪些工具
    def exposed_specs(self) -> list:
        """默认只给只读工具。写盘和执行要显式开。"""
        return [
            spec
            for spec in self.registry.specs()
            if self.allow_write or spec.risk == "read"
        ]

    # ---------------------------------------------------------------- 消息分发
    async def handle_message(self, raw: str) -> str | None:
        """处理一行报文，返回要回写的一行（通知返回 None）。

        拆成「输入一个字符串、输出一个字符串」是为了可测：测试不用起管道、
        不用起子进程，直接喂字符串断言输出。传输和协议是两件事。
        """
        request_id = None
        try:
            message = parse_message(raw)
            request_id = message.get("id")

            if is_notification(message):
                await self._handle_notification(message)
                return None  # ★通知绝对不能回包

            result = await self._dispatch(message["method"], message.get("params") or {})
            return make_result(request_id, result)

        except JsonRpcError as exc:
            return make_error(request_id, exc)
        except Exception as exc:  # noqa: BLE001 - 一条坏消息不能把 server 带走
            log.exception("处理消息时未捕获的异常")
            return make_error(
                request_id, JsonRpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            )

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method == "notifications/initialized":
            self._initialized = True
            log.info("客户端握手完成")
        else:
            # 通知不认识就静默忽略 —— 协议要求如此，不能回 error。
            log.debug("忽略未知通知: %s", method)

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [describe_tool(s) for s in self.exposed_specs()]}
        if method == "tools/call":
            return await self._call_tool(params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"未知方法: {method}")

    # ------------------------------------------------------------------ 各方法
    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手。声明自己有什么能力，客户端据此决定发什么请求。

        我们只声明 `tools`。resources / prompts 没实现就别声明 ——
        声明了不实现，客户端会发过来然后拿到 method not found。
        """
        client = (params.get("clientInfo") or {}).get("name", "unknown")
        log.info("initialize，来自客户端: %s", client)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "tools/call 缺少 name")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "arguments 必须是对象")

        # 没暴露的工具一律当作不存在，不要泄露「有这个工具但你没权限」。
        if name not in {spec.name for spec in self.exposed_specs()}:
            raise JsonRpcError(INVALID_PARAMS, f"未知工具: {name}")

        # 注册表已经包了超时、并发上限和异常降级，这里不用再套一层。
        result = await self.registry.call(name, self.workspace, **arguments)

        # ★工具失败走 result.isError，不是 JSON-RPC error。
        # 目的是让模型看得见错误内容，从而能改参数重试。
        text = result.content if result.ok else (result.error or "工具执行失败")
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not result.ok,
        }

    # ------------------------------------------------------------------ 传输层
    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """换行分隔的 JSON-RPC 循环。读到 EOF（对端关闭 stdin）就退出。"""
        log.info(
            "MCP server 就绪 | workspace=%s | 暴露 %s 个工具 | allow_write=%s",
            self.workspace.root,
            len(self.exposed_specs()),
            self.allow_write,
        )
        while True:
            line = await reader.readline()
            if not line:  # EOF
                log.info("对端关闭，退出")
                return
            raw = line.decode("utf-8").strip()
            if not raw:
                continue  # 空行忽略，别当成 parse error

            response = await self.handle_message(raw)
            if response is not None:
                writer.write(response.encode("utf-8") + b"\n")
                await writer.drain()
