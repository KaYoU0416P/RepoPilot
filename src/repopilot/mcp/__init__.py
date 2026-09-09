"""MCP（Model Context Protocol）server。手写 JSON-RPC 2.0，不引 SDK。"""

from repopilot.mcp.protocol import JsonRpcError
from repopilot.mcp.schema import describe_tool, input_schema
from repopilot.mcp.server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, MCPServer

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "JsonRpcError",
    "MCPServer",
    "describe_tool",
    "input_schema",
]
