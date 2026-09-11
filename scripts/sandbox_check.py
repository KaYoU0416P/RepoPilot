"""容器沙箱的隔离自检：把一份「攻击性」测试丢进沙箱，看每道墙是不是真的在。

    uv run python scripts/sandbox_check.py            # 用 docker 沙箱
    uv run python scripts/sandbox_check.py --local    # 对照组：本地子进程

**为什么要有这个脚本**：`--network none` 这类 flag 少写一条不会报错，
只会**静悄悄地不设防**。安全声明必须能被跑出来，不能只写在注释里。

对照着跑一遍最有说服力：同一份探针，`--local` 那组会看到联网成功、
宿主机文件可见 —— 那正是「本地子进程不是隔离」的直接证据。
"""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from repopilot.config import PROJECT_ROOT, get_settings
from repopilot.observability import setup_logging

#: 丢进沙箱去跑的探针。每个函数试一种越狱，打印 BLOCKED 或 ESCAPED。
PROBE = '''
import os, socket, sys

#: ★这几条路径由**宿主机**算好再字面量插进来，探针里绝不写 `expanduser`。
#: 踩过：容器里 `HOME=/tmp`，`expanduser("~/.ssh")` 会变成 `/tmp/.ssh` ——
#: 于是两组跑的根本不是同一个路径，"看不见"当然看不见，它压根不存在。
#: **对照实验的两组必须只差被测的那一个变量**，路径这种东西不能让它跟着环境漂。
HOST_ONLY = __HOST_ONLY__


def test_network():
    """断网。模型生成的代码不能外传，也不能下载东西回来。"""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        print("ESCAPED  联网成功")
    except OSError as exc:
        print(f"BLOCKED  联网被拦: {type(exc).__name__}")


def test_host_files_invisible():
    """宿主机的路径在容器里必须根本不存在。

    ★注意别拿 /etc/shadow 当探针 —— 那是**容器自己**的文件，任何 Linux 镜像
    里都有，看得见不代表越狱。要探的是**只有宿主机才有**的路径。
    """
    for path in HOST_ONLY:
        print(f"{'ESCAPED  看得见' if os.path.exists(path) else 'BLOCKED  看不见'}: {path}")


def test_cannot_read_shadow():
    """容器自己的 /etc/shadow 也读不了 —— 因为我们不是 root。"""
    try:
        with open("/etc/shadow") as handle:
            handle.read()
        print("ESCAPED  读到了 /etc/shadow")
    except OSError as exc:
        print(f"BLOCKED  读 /etc/shadow: {type(exc).__name__}")


def test_write_root():
    try:
        with open("/evil.txt", "w") as handle:
            handle.write("x")
        print("ESCAPED  根目录可写")
    except OSError as exc:
        print(f"BLOCKED  根目录只读: {type(exc).__name__}")


def test_workspace_is_writable():
    """反过来验一条：workspace 必须**可写**，否则 Agent 什么也干不了。
    安全不是把所有事都禁掉。"""
    with open("write-probe.txt", "w") as handle:
        handle.write("ok")
    os.remove("write-probe.txt")
    print("INFO     workspace 可写（应该的）")


def test_identity():
    print(f"INFO     uid={os.getuid()} cwd={os.getcwd()} python={sys.executable}")
'''


async def main() -> int:
    parser = argparse.ArgumentParser(description="沙箱隔离自检")
    parser.add_argument("--local", action="store_true", help="对照组：走本地子进程")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()

    # 在**宿主机**上算好"只有宿主机才有"的那几条路径，再字面量塞进探针。
    # 两组跑的必须是同一批绝对路径，否则对照不成立 —— 见 PROBE 里的注释。
    host_only = (
        "/Users" if sys.platform == "darwin" else str(Path.home().parent),
        str(Path.home() / ".ssh"),
        str(PROJECT_ROOT / ".env"),
    )
    probe = PROBE.replace("__HOST_ONLY__", repr(host_only))

    with tempfile.TemporaryDirectory(prefix="sandbox-check-") as tmp:
        workdir = Path(tmp)
        (workdir / "test_probe.py").write_text(probe, encoding="utf-8")

        command = [sys.executable, "-m", "pytest", "-q", "-s", "--no-header",
                   "-p", "no:cacheprovider"]

        if args.local:
            from repopilot.sandbox.local import run_command
            label = "本地子进程（对照组）"
            result = await run_command(command, cwd=workdir, timeout=120)
        else:
            from repopilot.sandbox.docker import run_command
            label = f"容器沙箱（{settings.sandbox_image}）"
            result = await run_command(command, cwd=workdir, timeout=180)

    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    body = f"{result.stdout}\n{result.stderr}"
    # pytest 的进度点（`.`）会粘在每个测试第一行输出的前面，
    # 所以按「包含」找而不是按「开头」找 —— 上一版就漏掉了三行。
    findings = []
    for line in body.splitlines():
        for mark in ("BLOCKED", "ESCAPED", "INFO"):
            if mark in line:
                findings.append(line[line.index(mark):])
                break
    for line in findings:
        print("  " + line)

    # ★从**解析出来的探针结论**里数，不要 `body.count("ESCAPED")`：
    # 测试一旦失败，pytest 会把源码行回显进输出，而源码里就有 "ESCAPED"
    # 这个字面量 —— 那会数出一个假的越狱。**安全检查自己误报，比不检查更糟。**
    escaped = sum(line.startswith("ESCAPED") for line in findings)
    print(f"\n越狱成功 {escaped} 项，耗时 {result.duration_ms}ms")
    if not args.local and escaped:
        print("⚠ 容器沙箱有洞，检查 build_docker_command 的 flag")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
