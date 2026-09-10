"""容器沙箱：把**不可信代码**关进一次性容器里跑。

`sandbox/local.py` 的隔离是「路径收敛 + 墙钟超时 + 杀进程组」—— 那是**应用层**
的约束，靠的是我们自己不写出破绽。但 `run_tests` 执行的是**模型生成的代码**，
它想 `import socket` 发一个请求、想读 `~/.ssh/id_rsa`、想 `while True: fork()`，
本地子进程一样都拦不住。这个模块把那道墙换成内核级的。

## 谁进容器，谁不进

`run_command` 加了一个 `untrusted=` 参数，**默认 False**。只有一处传 True：
`tools/test_runner.py`（跑目标仓库的测试）。

为什么不是"全都进容器"：`publishing/github.py` 的 `git clone/push` **需要网络
和凭证**，而容器是 `--network none` 的。把它一起塞进去只会得到一个必然失败的
发布链路。**沙箱是给不可信代码的，不是给我们自己的命令的** —— 参数名就是这个
安全模型本身，看到 `untrusted=True` 就知道这行在跑别人的代码。

## 拦住了什么（本地子进程拦不住的）

| 攻击 | 靠什么拦 |
|---|---|
| 联网外传 / 下载后门 | `--network none` ★ 最大的那一条 |
| 读宿主机文件（`~/.ssh`、`.env`） | 只挂载 workspace，别的看不见 |
| 吃光内存 | `--memory` |
| fork 炸弹 | `--pids-limit` |
| 提权 | `--cap-drop ALL` + `--security-opt no-new-privileges` |
| 写坏宿主机 | 根文件系统 `--read-only`，只有 `/work` 和 `/tmp` 可写 |

## 两个必然会踩的坑

**1. 宿主机路径在容器里不存在。**
`run_tests` 用的是 `sys.executable`（比如 `/Users/…/.venv/bin/python3`）——
容器里没有这个文件。workspace 也一样：宿主机是 `/Users/…/.workspaces/xxx`，
容器里是 `/work`。所以命令要**翻译**，见 `_translate`。
**bind mount 不等于同一个文件系统**，路径必须重映射。

**2. 杀掉 `docker run` 客户端，容器还在跑。**
`docker run` 只是个客户端，进程被 kill 之后容器继续在 daemon 里活着 ——
超时就此失效，而且泄漏一个还在烧 CPU 的容器。所以必须给容器**起名字**，
超时时显式 `docker kill <name>`。本地沙箱那边杀进程组就够了，这里不够。
"""

import asyncio
import os
import time
import uuid
from pathlib import Path

from repopilot.observability import get_logger
from repopilot.sandbox.local import CommandResult, _truncate

log = get_logger(__name__)

#: 容器里 workspace 的挂载点。命令和输出里的宿主机路径都要翻译成它。
WORKDIR = "/work"

#: 认得出「这是个 Python 解释器」的后缀。宿主机的绝对路径在容器里没有意义，
#: 统一换成容器自带的 `python3`。
_PY_NAMES = {"python", "python3"}


def _translate(command: list[str], host_cwd: Path) -> list[str]:
    """把宿主机路径改写成容器里的路径。

    只做两件事，刻意不做更多 —— 路径改写是**猜**，猜多了会在某天悄悄改错一个
    参数。这两条是必需且可判定的：解释器和 workspace 本身。
    """
    out = []
    for part in command:
        name = Path(part).name
        if part.startswith("/") and (name in _PY_NAMES or name.startswith("python3.")):
            out.append("python3")
        elif part.startswith(str(host_cwd)):
            out.append(part.replace(str(host_cwd), WORKDIR, 1))
        else:
            out.append(part)
    return out


def build_docker_command(
    command: list[str],
    *,
    cwd: Path,
    image: str,
    container_name: str,
    env: dict[str, str] | None = None,
    memory: str = "512m",
    cpus: str = "2",
    pids_limit: int = 256,
) -> list[str]:
    """拼出 `docker run …`。抽成纯函数，**安全参数可以被测试逐条断言** ——
    这些 flag 每一条都是一道墙，少一条不会报错，只会静悄悄地不设防。
    """
    args = [
        "docker", "run", "--rm",
        "--name", container_name,
        # ★最重要的一条：断网。模型生成的代码没法外传，也没法下载东西回来。
        "--network", "none",
        # memory-swap 必须等于 memory，否则它会拿 swap 继续吃，限制形同虚设。
        "--memory", memory, "--memory-swap", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids_limit),          # fork 炸弹
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # 根文件系统只读；要写的地方显式开：/work 是 bind mount 不受影响，
        # /tmp 给 pytest 之类的临时文件。
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m,mode=1777",
        # ★用宿主机的 uid 跑。不这么做的话容器里是 root，它在 bind mount 上
        # 建的文件在宿主机上属主是 root —— 然后 workspace 清理会失败。
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{cwd}:{WORKDIR}",
        "-w", WORKDIR,
    ]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    # HOME 指向可写的 /tmp：容器里 uid 没有 passwd 条目，很多工具会去写 ~。
    args += ["-e", "HOME=/tmp", image]
    return args + _translate(command, cwd)


async def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 64_000,
    image: str | None = None,
) -> CommandResult:
    """在一次性容器里跑命令。签名和 `local.run_command` 保持一致。"""
    from repopilot.config import get_settings

    name = f"repopilot-{uuid.uuid4().hex[:12]}"
    docker_command = build_docker_command(
        command,
        cwd=cwd,
        image=image or get_settings().sandbox_image,
        container_name=name,
        env=env,
    )

    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *docker_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        # ★杀客户端不够，容器还活着。必须点名杀。
        await _docker_kill(name)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            stdout, stderr = b"", b""

    duration_ms = int((time.perf_counter() - started) * 1000)
    result = CommandResult(
        command=command,
        exit_code=-1 if timed_out else (proc.returncode or 0),
        # 输出里的 /work 换回宿主机路径，否则 Agent 看到的报错路径它自己读不到。
        stdout=_truncate(stdout, max_output_bytes).replace(WORKDIR, str(cwd)),
        stderr=_truncate(stderr, max_output_bytes).replace(WORKDIR, str(cwd)),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
    log.info(
        "docker cmd %s exit=%s timed_out=%s %dms",
        " ".join(command[:3]),
        result.exit_code,
        timed_out,
        duration_ms,
    )
    return result


async def _docker_kill(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:  # noqa: BLE001 - 清理失败不该盖掉原来的超时错误
        log.warning("docker kill %s 失败，可能留下一个孤儿容器", name)
