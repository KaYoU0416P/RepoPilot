"""容器沙箱。

**大部分测试不需要 Docker**：`build_docker_command` 是纯函数，
每一条安全 flag 都能被单独断言。这很重要 —— 这些 flag 少写一条不会报错，
**只会静悄悄地不设防**，所以必须逐条钉住，而不是"跑一次看着像是对的"。

真的起容器的那几条标了 `slow`，没装 Docker 会 skip。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.sandbox import run_command
from repopilot.sandbox.docker import WORKDIR, _translate, build_docker_command


def _args(**kw) -> list[str]:
    return build_docker_command(
        kw.pop("command", ["python3", "-m", "pytest"]),
        cwd=kw.pop("cwd", Path("/host/ws")),
        image=kw.pop("image", "img:tag"),
        container_name=kw.pop("container_name", "c1"),
        **kw,
    )


def _flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# ====================================================== 每一道墙单独钉一条
def test_the_container_has_no_network():
    """★最重要的一条：断网。模型生成的代码不能外传，也不能下载后门回来。

    对照组实测：本地子进程里同一份探针**联网成功**，还能读到 `.env` 里的
    API key —— 那不是理论风险，是 `scripts/sandbox_check.py --local` 跑出来的。
    """
    assert _flag_value(_args(), "--network") == "none"


def test_swap_is_capped_together_with_memory():
    """★只设 --memory 不设 --memory-swap，容器会拿 swap 接着吃，限制形同虚设。"""
    args = _args(memory="256m")
    assert _flag_value(args, "--memory") == "256m"
    assert _flag_value(args, "--memory-swap") == "256m"


def test_pids_are_limited_against_fork_bombs():
    assert _flag_value(_args(pids_limit=64), "--pids-limit") == "64"


def test_all_capabilities_are_dropped():
    assert _flag_value(_args(), "--cap-drop") == "ALL"
    assert _flag_value(_args(), "--security-opt") == "no-new-privileges"


def test_root_filesystem_is_read_only_but_tmp_is_writable():
    """只读根 + 一个显式的 tmpfs：pytest 要写临时文件，但不能写别处。"""
    args = _args()
    assert "--read-only" in args
    assert _flag_value(args, "--tmpfs").startswith("/tmp:rw")


def test_the_container_runs_as_the_host_user():
    """★不这么做的话容器里是 root，它在 bind mount 上建的文件属主是 root，
    然后**宿主机清理 workspace 会失败**。这是 bind mount 最经典的坑。"""
    import os

    assert _flag_value(_args(), "--user") == f"{os.getuid()}:{os.getgid()}"


def test_only_the_workspace_is_mounted():
    """挂进去的必须**只有** workspace —— 宿主机别的地方一律看不见。"""
    args = _args(cwd=Path("/host/ws"))
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert mounts == [f"/host/ws:{WORKDIR}"]


def test_the_container_is_named_so_it_can_be_killed():
    """★杀掉 `docker run` 客户端，容器还活着 —— 超时就此失效，还漏一个
    在烧 CPU 的容器。必须点名 kill，所以必须先起名字。"""
    assert _flag_value(_args(container_name="repopilot-abc"), "--name") == "repopilot-abc"
    assert "--rm" in _args()


def test_env_vars_are_passed_through():
    args = _args(env={"PYTHONDONTWRITEBYTECODE": "1"})
    assert "-e" in args
    assert "PYTHONDONTWRITEBYTECODE=1" in args
    assert "HOME=/tmp" in args, "容器里的 uid 没有 passwd 条目，HOME 要指向可写处"


# ============================================== 宿主机路径在容器里不存在
def test_the_host_interpreter_path_is_rewritten():
    """★`sys.executable` 是 `/Users/…/.venv/bin/python3` —— 容器里没有这个文件。

    **bind mount 不等于同一个文件系统**，路径必须重映射。
    """
    out = _translate(["/Users/me/.venv/bin/python3", "-m", "pytest"], Path("/host/ws"))
    assert out[0] == "python3"
    assert out[1:] == ["-m", "pytest"]


def test_versioned_interpreters_are_rewritten_too():
    assert _translate(["/usr/bin/python3.12"], Path("/x"))[0] == "python3"


def test_paths_inside_the_workspace_are_remapped():
    out = _translate(["python3", "/host/ws/test_a.py"], Path("/host/ws"))
    assert out[1] == f"{WORKDIR}/test_a.py"


def test_unrelated_arguments_are_left_alone():
    """路径改写是**猜**，猜多了会在某天悄悄改错一个参数。只动能判定的两类。"""
    args = ["python3", "-m", "pytest", "-q", "--no-header", "-k", "not slow"]
    assert _translate(args, Path("/host/ws")) == args


# ======================================================== 谁进容器谁不进
async def test_trusted_commands_never_go_into_the_container(monkeypatch, tmp_path):
    """★`git clone/push` 需要网络和凭证，而容器是断网的。

    把它一起关进去只会得到一条必然失败的发布链路。
    **沙箱是给不可信代码的，不是给我们自己的命令的。**
    """
    monkeypatch.setattr("repopilot.config.get_settings", lambda: Settings(sandbox="docker"))
    called = {}

    async def fake_docker(*a, **kw):
        called["docker"] = True

    monkeypatch.setattr("repopilot.sandbox.docker.run_command", fake_docker)

    # 默认 untrusted=False → 走本地，不进容器
    result = await run_command([sys.executable, "-c", "print(1)"], cwd=tmp_path, timeout=30)
    assert "docker" not in called
    assert result.stdout.strip() == "1"


async def test_local_sandbox_ignores_the_untrusted_flag(monkeypatch, tmp_path):
    """`sandbox=local` 时 `untrusted=True` 也走子进程 —— 保持原有行为，
    让没装 Docker 的机器照样能跑 `make test`。"""
    monkeypatch.setattr("repopilot.config.get_settings", lambda: Settings(sandbox="local"))
    result = await run_command(
        [sys.executable, "-c", "print(2)"], cwd=tmp_path, timeout=30, untrusted=True
    )
    assert result.stdout.strip() == "2"


# =========================================== 真的起容器（没 Docker 就跳过）
def _docker_ready(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", image], capture_output=True, timeout=30, check=False
    )
    return probe.returncode == 0


IMAGE = Settings().sandbox_image
needs_docker = pytest.mark.skipif(
    not _docker_ready(IMAGE), reason=f"没有 Docker 或镜像 {IMAGE}（先跑 make sandbox-image）"
)


@pytest.mark.slow
@needs_docker
async def test_the_container_really_cannot_reach_the_network(tmp_path):
    """★这条是整个模块的重点：安全声明必须能被**跑出来**，不能只写在注释里。"""
    from repopilot.sandbox.docker import run_command as docker_run

    (tmp_path / "probe.py").write_text(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('ESCAPED')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n",
        encoding="utf-8",
    )
    result = await docker_run([sys.executable, "probe.py"], cwd=tmp_path, timeout=120)
    assert "BLOCKED" in result.stdout, result.stdout + result.stderr


@pytest.mark.slow
@needs_docker
async def test_files_written_in_the_container_belong_to_the_host_user(tmp_path):
    """`--user` 那条 flag 的实际后果：写出来的文件宿主机能删。"""
    import os

    from repopilot.sandbox.docker import run_command as docker_run

    await docker_run(
        [sys.executable, "-c", "open('made-inside.txt','w').write('x')"],
        cwd=tmp_path,
        timeout=120,
    )
    made = tmp_path / "made-inside.txt"
    assert made.exists()
    assert made.stat().st_uid == os.getuid(), "容器里是 root 的话，宿主机清不掉这个文件"
