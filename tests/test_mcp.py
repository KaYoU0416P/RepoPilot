"""MCP server 的协议契约。

不起子进程、不连数据库：`handle_message` 是「进一个字符串、出一个字符串」，
所以协议层可以被当纯函数测。传输层单独测一次（用内存流跑完整循环）。
"""

import asyncio
import json

import pytest

from repopilot.mcp import MCPServer
from repopilot.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from repopilot.mcp.schema import input_schema
from repopilot.tools import build_registry


@pytest.fixture
def server(workspace):
    return MCPServer(build_registry(), workspace)


@pytest.fixture
def rw_server(workspace):
    return MCPServer(build_registry(), workspace, allow_write=True)


def request(method: str, params: dict | None = None, request_id: int = 1) -> str:
    body: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body)


async def call(server: MCPServer, method: str, params: dict | None = None, request_id: int = 1):
    raw = await server.handle_message(request(method, params, request_id))
    return json.loads(raw)


# ================================================================== 握手
async def test_initialize_returns_server_info_and_capabilities(server):
    body = await call(server, "initialize", {"clientInfo": {"name": "pytest"}})
    result = body["result"]

    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert result["serverInfo"]["name"] == "repopilot"
    assert "protocolVersion" in result
    # 只声明真的实现了的能力。声明了不实现，客户端发过来就是 method not found。
    assert set(result["capabilities"]) == {"tools"}


async def test_ping_is_answered(server):
    assert (await call(server, "ping"))["result"] == {}


async def test_initialized_notification_gets_no_reply(server):
    """★通知没有 id，绝对不能回包 —— 回了对端会收到一个它没发过的响应。"""
    raw = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert await server.handle_message(raw) is None
    assert server._initialized is True


async def test_unknown_notification_is_silently_ignored(server):
    raw = json.dumps({"jsonrpc": "2.0", "method": "notifications/somethingElse"})
    assert await server.handle_message(raw) is None


# ============================================================ 协议层错误
async def test_malformed_json_is_a_parse_error(server):
    body = json.loads(await server.handle_message("{不是 JSON"))
    assert body["error"]["code"] == PARSE_ERROR


async def test_missing_jsonrpc_version_is_an_invalid_request(server):
    body = json.loads(await server.handle_message(json.dumps({"id": 1, "method": "ping"})))
    assert body["error"]["code"] == INVALID_REQUEST


async def test_unknown_method_is_method_not_found(server):
    body = await call(server, "resources/list")
    assert body["error"]["code"] == METHOD_NOT_FOUND


async def test_the_request_id_is_echoed_back_on_errors(server):
    """客户端靠 id 把响应和请求对上。错误响应丢了 id，对端就永远等下去。"""
    body = await call(server, "nope", request_id=99)
    assert body["id"] == 99


# ================================================================ tools/list
async def test_tools_list_only_exposes_read_tools_by_default(server):
    """默认不给写盘和执行。MCP 客户端是外部的，run_tests 会跑仓库里的代码。"""
    names = {t["name"] for t in (await call(server, "tools/list"))["result"]["tools"]}
    assert names == {"list_files", "read_file", "search_code", "git_diff"}
    assert "write_file" not in names
    assert "run_tests" not in names


async def test_allow_write_exposes_the_dangerous_tools(rw_server):
    names = {t["name"] for t in (await call(rw_server, "tools/list"))["result"]["tools"]}
    assert {"write_file", "run_tests"} <= names


async def test_every_tool_advertises_its_risk_level(server):
    """描述里带 risk 标签：让对面清楚这个调用会不会写盘、会不会执行代码。"""
    for tool in (await call(server, "tools/list"))["result"]["tools"]:
        assert "[risk=" in tool["description"]


# ============================================================== inputSchema
def test_schema_marks_parameters_without_defaults_as_required(registry):
    schema = input_schema(registry.get("read_file"))
    assert schema["required"] == ["path"]
    assert schema["properties"]["path"]["type"] == "string"


def test_schema_treats_defaulted_parameters_as_optional(registry):
    schema = input_schema(registry.get("list_files"))
    assert "required" not in schema
    assert schema["properties"]["glob"]["default"] == "**/*"


def test_schema_infers_types_from_annotations(registry):
    """Schema 的真相来源是函数签名，不是那份人读的说明字典。"""
    schema = input_schema(registry.get("search_code"))
    assert schema["properties"]["max_results"]["type"] == "integer"
    assert schema["properties"]["pattern"]["type"] == "string"


def test_schema_never_exposes_the_workspace_parameter(registry):
    """★workspace 由服务端注入。让客户端指定它 = 路径收敛被整个绕过去。"""
    for spec in registry.specs():
        assert "workspace" not in input_schema(spec)["properties"]


def test_schema_forbids_extra_properties(registry):
    assert input_schema(registry.get("read_file"))["additionalProperties"] is False


# ================================================================ tools/call
async def test_calling_a_tool_returns_text_content(server):
    body = await call(server, "tools/call", {"name": "list_files", "arguments": {}})
    result = body["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert "calculator.py" in result["content"][0]["text"]


async def test_tool_arguments_are_passed_through(server):
    body = await call(
        server, "tools/call", {"name": "read_file", "arguments": {"path": "calculator.py"}}
    )
    assert "def divide" in body["result"]["content"][0]["text"]


async def test_a_failing_tool_is_a_result_not_an_rpc_error(server):
    """★最重要的一条：工具失败是**业务结果**，不是协议失败。

    回成 JSON-RPC error 的话，模型看不到错误内容，也就没法改了重试。
    """
    body = await call(
        server, "tools/call", {"name": "read_file", "arguments": {"path": "不存在.py"}}
    )
    assert "error" not in body  # 不是 RPC 错误
    assert body["result"]["isError"] is True
    assert "不存在.py" in body["result"]["content"][0]["text"]


async def test_path_escape_is_blocked_and_reported_as_a_tool_error(server):
    """路径收敛在 MCP 这条路上同样生效 —— 它在注册表里，不在调用方。"""
    body = await call(
        server, "tools/call", {"name": "read_file", "arguments": {"path": "../../etc/passwd"}}
    )
    assert body["result"]["isError"] is True
    assert "sandbox" in body["result"]["content"][0]["text"]


async def test_hidden_tools_look_like_they_do_not_exist(server):
    """不暴露的工具要当作不存在，别泄露「有但你没权限」。"""
    body = await call(
        server, "tools/call", {"name": "write_file", "arguments": {"path": "x", "content": "y"}}
    )
    assert body["error"]["code"] == INVALID_PARAMS
    assert "未知工具" in body["error"]["message"]


async def test_write_tool_works_when_allowed(rw_server, workspace):
    body = await call(
        rw_server,
        "tools/call",
        {"name": "write_file", "arguments": {"path": "new.py", "content": "x = 1\n"}},
    )
    assert body["result"]["isError"] is False
    assert (workspace.root / "new.py").read_text() == "x = 1\n"


async def test_bad_arguments_are_reported_without_crashing(server):
    """模型编了个不存在的参数。注册表把 TypeError 降级成 ToolResult，不该炸。"""
    body = await call(
        server, "tools/call", {"name": "read_file", "arguments": {"路径": "calculator.py"}}
    )
    assert body["result"]["isError"] is True


async def test_tools_call_without_a_name_is_invalid_params(server):
    body = await call(server, "tools/call", {"arguments": {}})
    assert body["error"]["code"] == INVALID_PARAMS


# ================================================================== 传输层
async def test_full_stdio_loop_over_in_memory_streams(server):
    """换行分隔的 JSON-RPC 循环：喂两条请求 + 一条通知，应该只回两条。"""
    reader = asyncio.StreamReader()
    reader.feed_data(
        (
            request("initialize", {}, 1)
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + request("tools/list", {}, 2)
            + "\n"
        ).encode()
    )
    reader.feed_eof()

    written: list[bytes] = []

    class FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

    await server.serve(reader, FakeWriter())

    lines = b"".join(written).decode().strip().split("\n")
    assert len(lines) == 2, "通知不能有响应"
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1])["id"] == 2


async def test_blank_lines_are_ignored_not_treated_as_parse_errors(server):
    reader = asyncio.StreamReader()
    reader.feed_data(b"\n\n" + request("ping").encode() + b"\n")
    reader.feed_eof()

    written: list[bytes] = []

    class FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

    await server.serve(reader, FakeWriter())
    assert len(b"".join(written).decode().strip().split("\n")) == 1
