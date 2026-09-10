"""沙箱入口。按配置把命令送去子进程还是容器。

**分派在这里而不是在调用方**：`run_command` 的签名是整个上层依赖的契约
（工具、发布链路、评测 harness 都直接用它），换实现不该让它们改一行。
"""

from pathlib import Path

from repopilot.sandbox.local import CommandResult
from repopilot.sandbox.local import run_command as _run_local


async def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 64_000,
    untrusted: bool = False,
) -> CommandResult:
    """跑一条命令。

    `untrusted=True` 表示**这行在跑别人的代码**（目前只有 `run_tests`），
    配了 `sandbox=docker` 时它才会进容器。默认 False —— `git clone/push`
    需要网络和凭证，而容器是断网的，把它一起关进去只会得到一条必然失败的
    发布链路。**沙箱是给不可信代码的，不是给我们自己的命令的。**

    参数名就是安全模型本身：看到 `untrusted=True` 就知道这行危险。
    """
    from repopilot.config import get_settings

    if untrusted and get_settings().sandbox == "docker":
        from repopilot.sandbox.docker import run_command as _run_docker

        return await _run_docker(
            command, cwd=cwd, timeout=timeout, env=env, max_output_bytes=max_output_bytes
        )
    return await _run_local(
        command, cwd=cwd, timeout=timeout, env=env, max_output_bytes=max_output_bytes
    )


__all__ = ["CommandResult", "run_command"]
