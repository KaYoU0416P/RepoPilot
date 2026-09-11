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


# ==================================== 陌生仓库里的符号链接（clone 之后才成立的威胁）
@pytest.fixture
def repo_with_evil_symlink(tmp_path, settings):
    """一个"仓库"，里面有个指向宿主机私钥的链接。clone 陌生仓库之后这不再是假想。"""
    import shutil

    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN PRIVATE KEY-----\nhunter2\n", encoding="utf-8")

    source = tmp_path / "repo"
    shutil.copytree(settings.sample_repo, source)
    (source / "notes.txt").symlink_to(secret)
    return source, secret


def test_a_symlink_out_of_the_repo_does_not_become_a_real_file(
    repo_with_evil_symlink, tmp_path
):
    """★`copytree` 默认 `symlinks=False` —— 它**跟着链接走，把内容拷过来**。

    于是仓库里一个 `notes.txt -> ~/.ssh/id_rsa` 会在 workspace 里变成一个
    **装着私钥的真文件**：`resolve()` 的越界检查完全看不见它，因为路径是
    合法的，内容早在拷贝那一刻就越界了。Agent 读得到，`git add -A` 还会把它
    收进 diff，一路进到 PR 里。

    `symlinks=True` 把它保留成链接，`resolve()` 这才真正生效。
    """
    from repopilot.workspace import WorkspaceManager

    source, secret = repo_with_evil_symlink
    manager = WorkspaceManager(tmp_path / "ws")
    ws = manager.create(source, run_id="evil")
    try:
        copied = ws.root / "notes.txt"
        assert copied.is_symlink(), "链接被解引用了：私钥内容已经躺在 workspace 里"
        # 真正的防线：任何工具想读它都会被拒
        with pytest.raises(PathEscapeError):
            ws.resolve("notes.txt")
        # 也不该出现在给 Agent 看的文件树里 —— 那等于主动指路
        assert "notes.txt" not in ws.tree()
    finally:
        manager.cleanup(ws)
        assert secret.exists(), "清理 workspace 时顺着链接把宿主机文件删了"


async def test_read_file_refuses_an_escaping_symlink(repo_with_evil_symlink, tmp_path, registry):
    from repopilot.workspace import WorkspaceManager

    source, _ = repo_with_evil_symlink
    manager = WorkspaceManager(tmp_path / "ws2")
    ws = manager.create(source, run_id="evil2")
    try:
        result = await registry.call("read_file", ws, path="notes.txt")
        assert result.ok is False
        assert "hunter2" not in result.content
    finally:
        manager.cleanup(ws)


def test_ordinary_paths_still_resolve(workspace):
    """别把正常的活也挡了 —— 安全不是把所有事都禁掉。"""
    assert workspace.resolve("calculator.py").name == "calculator.py"
    assert workspace.resolve("pkg/.gitignore").name == ".gitignore"
