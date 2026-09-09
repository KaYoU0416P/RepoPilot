"""`ToolSpec` → MCP 的 `inputSchema`（JSON Schema）。

MCP 要求每个工具声明一份 JSON Schema，客户端拿它去约束模型的调用参数。
我们的 `ToolSpec.parameters` 是给人看的说明（`{"path": "str, repo-relative"}`），
不是 Schema，所以要转。

转换的**真相来源是函数签名**，不是那个说明字典：

    async def read_file(workspace, path: str) -> ToolResult
                                 ^^^^^^^^^
    有类型注解 → 类型；没有默认值 → required

说明字典只贡献 `description`。这样做的好处是签名改了 Schema 自动跟着变，
不会出现「函数加了个参数、Schema 忘了改」的漂移 —— 那种 bug 只会在模型
调用时才暴露，很难查。
"""

import inspect
from typing import Any

from repopilot.tools import ToolSpec

#: Python 注解 → JSON Schema 的 type。只覆盖工具实际用到的几种，
#: 不认识的一律退回 "string"（模型传字符串总是安全的）。
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

#: 工具函数的第一个参数是 Workspace，由服务端注入，**绝不暴露给客户端**。
#: 让客户端指定 workspace 等于把路径收敛整个绕过去了。
_INJECTED = {"workspace"}


def input_schema(spec: ToolSpec) -> dict[str, Any]:
    """生成一个 object 类型的 JSON Schema。"""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in inspect.signature(spec.fn).parameters.items():
        if name in _INJECTED or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        field: dict[str, Any] = {"type": _JSON_TYPES.get(param.annotation, "string")}
        description = spec.parameters.get(name)
        if description:
            field["description"] = description
        if param.default is not inspect.Parameter.empty:
            field["default"] = param.default
        else:
            required.append(name)
        properties[name] = field

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    # 不允许多余字段：模型编出一个不存在的参数时，希望在客户端就被挡下来，
    # 而不是等到我们这边抛 TypeError。
    schema["additionalProperties"] = False
    return schema


def describe_tool(spec: ToolSpec) -> dict[str, Any]:
    """一个工具在 `tools/list` 里的样子。

    描述里带上 risk 标签，是给对面的人/模型一个明确信号：
    这个调用会不会写盘、会不会执行代码。
    """
    return {
        "name": spec.name,
        "description": f"{spec.description} [risk={spec.risk}]",
        "inputSchema": input_schema(spec),
    }
