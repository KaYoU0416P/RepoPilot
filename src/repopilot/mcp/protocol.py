"""JSON-RPC 2.0 的最小实现。**手写，不引第三方 SDK。**

为什么手写：MCP 就是「JSON-RPC 2.0 + 一组约定好的方法名」。协议本身只有
几十行，引一个 SDK 之后你就说不清楚握手到底发生了什么、错误码怎么分层。
这个项目的卖点是「知道自己在干什么」，不是「装了什么包」。

JSON-RPC 2.0 只有三种报文：

    请求      {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
    响应      {"jsonrpc":"2.0","id":1,"result":{...}}
              {"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"..."}}
    通知      {"jsonrpc":"2.0","method":"notifications/initialized"}   ← 没有 id

**有没有 `id` 是唯一的区分点**，而且它决定了要不要回包：通知**绝对不能回**，
回了对端会当成一个它没发过的响应。

Java 对照：可以理解成一个文本版的极简 RPC。和 gRPC/Dubbo 的区别是没有 IDL、
没有代码生成、传输层可以是任何双向字节流（这里是进程的 stdin/stdout）。
"""

import json
from typing import Any

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 规定的标准错误码。别自己发明，客户端是按这些码分支的。
PARSE_ERROR = -32700  # 收到的不是合法 JSON
INVALID_REQUEST = -32600  # 是 JSON，但不是合法的 JSON-RPC 报文
METHOD_NOT_FOUND = -32601  # 方法名不认识
INVALID_PARAMS = -32602  # 方法认识，参数不对
INTERNAL_ERROR = -32603  # 服务端自己炸了


class JsonRpcError(Exception):
    """需要以 JSON-RPC error 形式回给对端的错误。

    抽成异常是为了让 handler 可以直接 `raise`，不用每层都返回错误对象再往上传。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def parse_message(raw: str) -> dict[str, Any]:
    """把一行文本解析成 JSON-RPC 报文，不合法就抛 JsonRpcError。"""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonRpcError(PARSE_ERROR, f"不是合法 JSON: {exc}") from exc

    if not isinstance(message, dict):
        raise JsonRpcError(INVALID_REQUEST, "报文必须是 JSON 对象")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise JsonRpcError(INVALID_REQUEST, f"jsonrpc 必须是 {JSONRPC_VERSION!r}")
    if not isinstance(message.get("method"), str):
        raise JsonRpcError(INVALID_REQUEST, "缺少 method")
    return message


def is_notification(message: dict[str, Any]) -> bool:
    """没有 id 就是通知。通知**不能**有响应。"""
    return "id" not in message


def make_result(request_id: Any, result: Any) -> str:
    return json.dumps(
        {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result},
        ensure_ascii=False,
    )


def make_error(request_id: Any, error: JsonRpcError) -> str:
    return json.dumps(
        {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error.to_dict()},
        ensure_ascii=False,
    )
