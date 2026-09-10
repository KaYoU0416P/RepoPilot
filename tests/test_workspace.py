import pytest

from repopilot.workspace import PathEscapeError


def test_workspace_is_a_copy(workspace, settings):
    assert workspace.root != settings.sample_repo
    assert (workspace.root / "calculator.py").is_file()


def test_resolve_allows_paths_inside(workspace):
    assert workspace.resolve("calculator.py").is_file()


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "/etc/passwd", "sub/../../../outside.py"],
)
def test_resolve_blocks_escape(workspace, bad):
    with pytest.raises(PathEscapeError):
        workspace.resolve(bad)


# ============================================ .git 是控制面，不是源码
@pytest.mark.parametrize(
    "path",
    [".git/config", ".git/hooks/pre-commit", "sub/../.git/config", ".git"],
)
def test_agent_cannot_touch_repository_metadata(workspace, path):
    """★堵的是一条**宿主机代码执行**，不是洁癖。

    `.git/` 在 workspace **里面**，所以「不许逃出 workspace」那条拦不住它。
    而 `git_diff` 工具是在**宿主机**上跑 `git add` 的 —— 只要往 `.git/config`
    写一行：

        [core] fsmonitor = /bin/sh -c '...'

    git 刷新索引时就会替 Agent 在宿主机上执行它。`.git/hooks/` 同理。

    **仓库元数据是 git 的控制面**，Agent 没有任何正当理由去碰它。
    """
    with pytest.raises(PathEscapeError):
        workspace.resolve(path)


async def test_write_file_refuses_to_write_into_dot_git(workspace, registry):
    """走一遍真正的工具入口，确认防线在调用链上、不只在 resolve() 里。"""
    result = await registry.call(
        "write_file",
        workspace,
        path=".git/config",
        content="[core]\n\tfsmonitor = /bin/sh -c 'echo pwned'\n",
    )
    assert result.ok is False
    assert "blocked by sandbox" in (result.error or "")
    assert not (workspace.root / ".git" / "config").read_text().count("fsmonitor")


def test_ordinary_paths_still_resolve(workspace):
    """别把正常的活也挡了 —— 安全不是把所有事都禁掉。"""
    assert workspace.resolve("calculator.py").name == "calculator.py"
    assert workspace.resolve("pkg/.gitignore").name == ".gitignore"
