"""clone 目标仓库。

**远端是本机上一个真的裸仓库**，所以 clone / fetch / reset 走的是真 git，
只有"它在 GitHub 上"这件事是假的。比整个 mock 掉 git 有价值得多 ——
要验的恰恰是 git 的真实行为（默认分支怎么找、token 会不会落进 .git/config）。
"""

import subprocess
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.workspace.repos import RepoCache, RepoError, _auth_env, parse_full_name


def _git(*args: str, cwd: Path) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    out = subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """一个本机裸仓库，冒充 GitHub 上的 `acme/widget`。"""
    work = tmp_path / "src"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "init", "--no-gpg-sign", cwd=work)

    bare = tmp_path / "remote" / "acme" / "widget.git"
    bare.parent.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    # 裸仓库默认没有 HEAD 指向……有的，clone --bare 会带过来。
    return bare


@pytest.fixture
def cache(tmp_path: Path, upstream: Path) -> RepoCache:
    settings = Settings(
        git_remote_base=str(upstream.parent.parent),  # …/remote，拼出 …/remote/acme/widget.git
        repo_cache_root=tmp_path / "repos",
        clone_timeout_seconds=60,
    )
    return RepoCache(settings)


# ======================================================= 名字校验（安全入口）
@pytest.mark.parametrize(
    "name",
    [
        "acme/../../etc",  # 路径穿越
        "../acme/widget",
        "acme/wid/get",  # 多一层
        "-oProxyCommand=evil/widget",  # ★以 - 开头 = git 会当成**选项**解析
        "acme/--upload-pack=touch",
        ".git/widget",
        "acme",  # 少一段
        "",
        "acme /widget",  # 空格
        "acme/widget;rm -rf /",
    ],
)
def test_malformed_repo_names_are_refused(name):
    """★`owner/repo` 来自 webhook payload，**同时**被拼成 URL 和文件系统路径。

    最容易被忽略的是 `-` 开头那一条：它不是路径问题，是**参数注入** ——
    `git clone -- <url> <dir>` 里少了 `--` 的话，`-oProxyCommand=...`
    会被 git 当成选项执行。所以首字符单独限制，而不只是 `[\\w.-]+`。
    """
    with pytest.raises(RepoError):
        parse_full_name(name)


def test_normal_names_parse():
    assert parse_full_name("acme/widget") == ("acme", "widget")
    assert parse_full_name("Some-Org/my_repo.v2") == ("Some-Org", "my_repo.v2")
    assert parse_full_name("acme/widget.git") == ("acme", "widget"), ".git 后缀不该进目录名"


def test_cache_path_is_a_pure_function_of_the_name(cache, tmp_path):
    """webhook 入队时仓库还没 clone，它必须能**先算出**将来的位置。"""
    path = cache.path_for("acme/widget")
    assert path == (tmp_path / "repos" / "acme" / "widget").resolve()
    assert cache.path_for("acme/widget") == path  # 确定性
    assert not path.exists(), "path_for 不该做任何 IO"


# =============================================================== token 不落盘
def test_the_token_never_lands_in_argv_or_on_disk():
    """★两种常见写法都会泄露 PAT，这条钉住我们用的是第三种。

    - 拼进 URL      → git 把它写进 `.git/config`，**留在磁盘上**
    - `git -c ...`  → 进程 argv **全机器可见**（`ps aux`）

    `GIT_CONFIG_COUNT/KEY/VALUE` 走环境变量：只对这个子进程生效、不落盘、
    不在别人的 `ps` 输出里。
    """
    settings = Settings(github_token="ghp_SECRET123", git_remote_base="https://github.com")
    env = _auth_env(settings)

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_SECRET123" not in env["GIT_CONFIG_VALUE_0"], "应该是 base64，不是明文"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")

    # clone 命令里的 URL 必须是干净的 —— 它会被 git 原样写进 .git/config
    cache = RepoCache(settings)
    assert cache.clone_url("acme/widget") == "https://github.com/acme/widget.git"
    assert "ghp_" not in cache.clone_url("acme/widget")


def test_no_auth_env_without_a_token():
    assert _auth_env(Settings(github_token="")) == {}


def test_an_auth_failure_says_that_public_repos_break_too():
    """★实测踩到的坑，写成测试钉住。

    拿一个假 token 去 clone **公开**仓库 `octocat/Hello-World`，结果是
    `remote: Invalid username or token` —— 只要发了 Authorization 头，
    GitHub 就按那个身份判，**不会因为仓库是公开的就退回匿名访问**。

    后果：PAT 一过期，所有 clone 一起挂，报错看起来却像仓库不存在或网络问题。
    **发凭证不是免费的：错的凭证比不发凭证更糟。**
    """
    cache = RepoCache(Settings(github_token="ghp_expired"))
    hint = cache._auth_hint("remote: Invalid username or token.")
    assert "公开仓库也会因此失败" in hint
    assert "留空" in hint

    # 别在无关的报错上乱给提示 —— 误导比不提示更浪费时间
    assert cache._auth_hint("Repository not found.") == ""
    assert RepoCache(Settings(github_token=""))._auth_hint("Invalid username or token") == ""


# ================================================================= 真的 clone
@pytest.mark.slow
async def test_first_run_clones_it(cache, tmp_path):
    path = await cache.ensure("acme/widget")
    assert (path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert cache.is_cached("acme/widget")


@pytest.mark.slow
async def test_second_run_reuses_the_cache_and_picks_up_upstream_changes(cache, upstream, tmp_path):
    """缓存必须是上游的**忠实镜像** —— 它是每个 workspace 的拷贝源头。"""
    first = await cache.ensure("acme/widget")
    marker = first / ".proof-not-recloned"
    marker.write_text("x", encoding="utf-8")

    # 上游前进一格
    work = tmp_path / "push-from"
    _git("clone", "-q", str(upstream), str(work), cwd=tmp_path)
    (work / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "bump", "--no-gpg-sign", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)

    second = await cache.ensure("acme/widget")
    assert second == first, "同一个仓库必须落在同一个目录，不能每次新建"
    assert (second / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n", "没 fetch 到新提交"
    assert not marker.exists(), "★git clean 没生效：残留会被拷进每一个 workspace"


@pytest.mark.slow
async def test_a_failed_clone_leaves_no_half_repo_behind(cache):
    """★clone 到临时目录再改名，为的就是这个。

    直接 clone 到最终路径的话，失败会在缓存里留下半份仓库，而
    `is_cached` 只看 `.git` 在不在 —— 下一个 run 会拿着残缺仓库干活。
    """
    with pytest.raises(RepoError):
        await cache.ensure("acme/does-not-exist")

    assert not cache.is_cached("acme/does-not-exist")
    parent = cache.path_for("acme/does-not-exist").parent
    leftovers = [p.name for p in parent.iterdir()] if parent.exists() else []
    assert leftovers == ["widget"] or leftovers == [], f"临时目录没清干净: {leftovers}"


# ================================= 闸门：没开容器沙箱就不准跑陌生仓库
def _row(**kw):
    from datetime import UTC, datetime
    from uuid import uuid4

    from repopilot.db.models import RunRow

    return RunRow(
        **{
            "id": uuid4(),
            "status": "queued",
            "task": "修个 bug",
            "repo_path": "/tmp/whatever",
            "source": "github_issue",
            "external_ref": "acme/widget#1",
            "created_at": datetime.now(UTC),
            **kw,
        }
    )


def _runner(settings):
    from repopilot.worker.bus import EventBus
    from repopilot.worker.runner import Runner

    return Runner(settings, EventBus())


async def test_a_remote_repo_is_refused_without_the_container_sandbox(tmp_path):
    """★把配置注释里那句警告变成**代码里的闸门**。

    clone 来的是陌生人的代码，跑它的测试就是在本机执行任意代码。本地子进程
    沙箱是应用层的 —— `make sandbox-check --local` 实测能联网、能读 `.env`。
    所以这里不是打条日志警告，是**拒绝执行**。
    能被违反而不报错的安全约定，等于不存在。
    """
    from repopilot.worker.runner import RunFailed

    runner = _runner(Settings(sandbox="local", workspace_root=tmp_path / "ws"))
    with pytest.raises(RunFailed, match="容器沙箱"):
        await runner._prepare_repo(_row(), lambda *a, **k: None)


async def test_a_local_repo_is_unaffected_by_the_gate(tmp_path):
    """demo / 评测 / 自己的仓库（source=manual）照常跑，不被这道闸门误伤。
    安全不是把所有事都禁掉。"""
    runner = _runner(Settings(sandbox="local", workspace_root=tmp_path / "ws"))
    path = await runner._prepare_repo(_row(source="manual"), lambda *a, **k: None)
    assert path == Path("/tmp/whatever"), "本地路径原样返回，不该去 clone"


async def test_a_broken_external_ref_fails_fast(tmp_path):
    from repopilot.worker.runner import RunFailed

    runner = _runner(Settings(sandbox="docker", workspace_root=tmp_path / "ws"))
    with pytest.raises(RunFailed, match="external_ref"):
        await runner._prepare_repo(_row(external_ref="不是个引用"), lambda *a, **k: None)


@pytest.mark.slow
async def test_concurrent_runs_on_the_same_repo_do_not_race(cache):
    """两个 run 同时命中同一个仓库：第二个等第一个做完，不是两个一起 reset。"""
    import asyncio

    paths = await asyncio.gather(*(cache.ensure("acme/widget") for _ in range(3)))
    assert len(set(paths)) == 1
    assert (paths[0] / "app.py").exists()
