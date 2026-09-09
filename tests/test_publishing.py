"""发布链路的契约：publishing → published。

测试策略是这个文件最值得说的地方：

  git 那半边  **不 mock**。造一个本地裸仓库当"远端"，clone / apply / commit /
              push 全是真的 git 命令。裸仓库和 GitHub 在 git 协议层面没区别，
              所以这里能测出「分支推上去了没有、提交内容对不对」。
  HTTP 那半边 用 httpx.MockTransport 换掉。GitHub API 的请求体断言得很细，
              但不联网、不要 token。

结论：唯一没被真实覆盖的只剩「GitHub 服务器本身怎么响应」。
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from repopilot.db import approvals as approvals_repo
from repopilot.db import runs as runs_repo
from repopilot.domain import RunStatus
from repopilot.github import GitHubClient
from repopilot.publishing import DryRunPublisher, GitHubPublisher, PublishError, branch_name
from repopilot.publishing.github import strip_diff_preamble
from repopilot.worker import EventBus, Worker

REPO = "kayou/demo-repo"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t.local",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t.local",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout


# ------------------------------------------------------------------ 夹具
@pytest.fixture
def origin(tmp_path):
    """一个本地裸仓库，扮演 GitHub 上的那个仓库。

    路径故意做成 `<base>/kayou/demo-repo.git`，这样 GitHubPublisher 用
    `f"{remote_base}/{repo}.git"` 拼出来的地址就能直接命中，
    生产和测试走的是**同一行拼接代码**。
    """
    bare = tmp_path / "remotes" / f"{REPO}.git"
    bare.parent.mkdir(parents=True)
    git("init", "--bare", "-q", "--initial-branch=main", str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def source_repo(tmp_path, origin):
    """开发者本地的那份 clone —— 也就是 runs.repo_path 指向的东西。"""
    work = tmp_path / "source"
    work.mkdir()
    git("init", "-q", "--initial-branch=main", cwd=work)
    (work / "calculator.py").write_text("def divide(a, b):\n    return a / b\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "init", "--no-gpg-sign", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return work


DIFF = """diff --git a/calculator.py b/calculator.py
index 1111111..2222222 100644
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,4 @@
 def divide(a, b):
+    if b == 0:
+        raise ValueError("除数不能为 0")
     return a / b
"""


@pytest.fixture
def api_calls():
    """记录所有打到 GitHub API 的请求，测试结束后断言。"""
    return []


@pytest.fixture
def github_client(api_calls):
    """假的 GitHub。默认路径：分支上没有已存在的 PR，创建成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        api_calls.append(request)
        path = request.url.path
        if path.endswith("/pulls") and request.method == "GET":
            return httpx.Response(200, json=[])  # 还没有 PR
        if path.endswith("/pulls") and request.method == "POST":
            return httpx.Response(
                201, json={"html_url": f"https://github.com/{REPO}/pull/7", "number": 7}
            )
        if path.endswith("/comments"):
            return httpx.Response(
                201, json={"html_url": f"https://github.com/{REPO}/issues/42#issuecomment-1"}
            )
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(404, json={"message": "not found"})

    transport = httpx.MockTransport(handler)
    return GitHubClient("fake-token", client=httpx.AsyncClient(transport=transport))


@pytest.fixture
def publisher(settings, origin, github_client, monkeypatch):
    monkeypatch.setattr(settings, "github_token", "fake-token")
    return GitHubPublisher(settings, github_client, remote_base=str(origin.parent.parent))


async def make_run(source_repo, *, status=RunStatus.PUBLISHING, diff=DIFF, external_ref=None):
    """造一个"已批准、待发布"的 run，直接推到目标状态。"""
    row = await runs_repo.create_run(
        task="divide 除数为 0 时崩溃",
        repo_path=str(source_repo),
        source="github_issue",
        external_ref=external_ref if external_ref is not None else f"{REPO}#42",
    )
    await runs_repo.transition(row.id, RunStatus.RUNNING)
    if status is RunStatus.RUNNING:
        return row
    await runs_repo.transition(
        row.id, RunStatus.PENDING_APPROVAL, diff=diff, files_changed=["calculator.py"]
    )
    if status is RunStatus.PENDING_APPROVAL:
        return await runs_repo.get_run(row.id)
    await approvals_repo.decide(row.id, decision="approved", decided_by="kayou")
    return await runs_repo.get_run(row.id)


# ================================================== 纯函数：diff 前言处理
def test_diffstat_preamble_is_stripped():
    """git_diff 工具带了 --stat，前面那段摘要不能喂给 git apply。"""
    raw = " calculator.py | 2 +-\n 1 file changed\n\n" + DIFF
    assert strip_diff_preamble(raw).startswith("diff --git ")


def test_clean_diff_is_left_alone():
    assert strip_diff_preamble(DIFF) == DIFF


def test_branch_name_is_deterministic():
    """幂等的前提：同一个 run 每次都算出同一个分支名。"""
    run_id = "0192abcd-1234-7890-abcd-ef0123456789"
    assert branch_name(run_id) == branch_name(run_id) == "repopilot/run-0192abcd"


# ================================================== GitHubPublisher（真 git）
pytestmark = pytest.mark.usefixtures("db")


async def test_publish_pushes_a_branch_with_the_fix(db, source_repo, origin, publisher):
    """最核心的一条：分支真的推到"远端"了，而且内容是 diff 里那段修复。"""
    row = await make_run(source_repo)
    result = await publisher.publish(row)

    branch = branch_name(row.id)
    assert result.branch == branch

    # 从裸仓库里把那个分支的文件读出来 —— 不看我们自己的返回值，看远端的事实
    content = git("show", f"{branch}:calculator.py", cwd=origin)
    assert "raise ValueError" in content

    # 分支是从 main 长出来的，不是一个孤立的历史
    assert git("rev-parse", "main", cwd=origin).strip() in git(
        "log", "--format=%H", branch, cwd=origin
    )


async def test_publish_opens_a_pr_and_comments_on_the_issue(source_repo, publisher, api_calls):
    row = await make_run(source_repo)
    result = await publisher.publish(row)

    assert result.pr_url == f"https://github.com/{REPO}/pull/7"

    created = next(r for r in api_calls if r.method == "POST" and r.url.path.endswith("/pulls"))
    body = json.loads(created.content)
    assert body["head"] == branch_name(row.id)
    assert body["base"] == "main"  # 从仓库读的默认分支，不是写死的
    assert "#42" in body["title"]
    assert "calculator.py" in body["body"]  # 改了哪些文件要写进 PR 正文

    comment = next(r for r in api_calls if r.url.path.endswith("/comments"))
    assert f"/{REPO}/pull/7" in json.loads(comment.content)["body"]


async def test_pat_is_sent_as_a_bearer_token(source_repo, publisher, api_calls):
    await publisher.publish(await make_run(source_repo))
    assert api_calls[0].headers["Authorization"] == "Bearer fake-token"


async def test_republishing_reuses_the_existing_pr(
    source_repo, settings, origin, api_calls, monkeypatch
):
    """崩溃重试的安全性：push 成功但开 PR 前挂了，重试不能开出第二个 PR。

    这里模拟"分支上已经有一个开着的 PR"，publisher 必须复用它。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        api_calls.append(request)
        if request.url.path.endswith("/pulls") and request.method == "GET":
            return httpx.Response(
                200, json=[{"html_url": f"https://github.com/{REPO}/pull/7", "number": 7}]
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"html_url": "https://github.com/c/1"})
        return httpx.Response(500, json={"message": "不该走到这里"})

    monkeypatch.setattr(settings, "github_token", "fake-token")
    transport = httpx.MockTransport(handler)
    client = GitHubClient("fake-token", client=httpx.AsyncClient(transport=transport))
    publisher = GitHubPublisher(settings, client, remote_base=str(origin.parent.parent))

    row = await make_run(source_repo)
    await publisher.publish(row)  # 第一次
    result = await publisher.publish(row)  # 重试

    assert result.pr_url == f"https://github.com/{REPO}/pull/7"
    assert not [r for r in api_calls if r.method == "POST" and r.url.path.endswith("/pulls")]


async def test_unapplicable_diff_raises_publish_error(source_repo, publisher):
    """diff 打不上（源仓库在 Agent 跑完之后又变了）→ 逻辑失败，不该重试。"""
    row = await make_run(source_repo, diff="diff --git a/nope.py b/nope.py\n@@ -1 +1 @@\n-x\n+y\n")
    with pytest.raises(PublishError, match="diff 打不上去"):
        await publisher.publish(row)


async def test_empty_diff_raises_publish_error(source_repo, publisher):
    row = await make_run(source_repo, diff="")
    with pytest.raises(PublishError, match="没有 diff"):
        await publisher.publish(row)


async def test_bad_external_ref_raises_publish_error(source_repo, publisher):
    row = await make_run(source_repo, external_ref="乱七八糟")
    with pytest.raises(PublishError, match="external_ref"):
        await publisher.publish(row)


async def test_client_error_becomes_publish_error_but_server_error_does_not(
    source_repo, settings, origin, monkeypatch
):
    """4xx = 我们的问题，重试无意义 → PublishError（标 failed）。
    5xx = 对方的问题，值得重试 → 原样抛出，交给租约机制重来。
    """
    monkeypatch.setattr(settings, "github_token", "fake-token")

    def make(status: int):
        def handler(request):
            if request.url.path.endswith("/pulls") and request.method == "GET":
                return httpx.Response(200, json=[])
            if request.url.path == f"/repos/{REPO}":
                return httpx.Response(200, json={"default_branch": "main"})
            return httpx.Response(status, json={"message": "boom"})

        client = GitHubClient("t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return GitHubPublisher(settings, client, remote_base=str(origin.parent.parent))

    from repopilot.github import GitHubError

    with pytest.raises(PublishError):
        await make(403).publish(await make_run(source_repo))
    with pytest.raises(GitHubError):
        await make(503).publish(await make_run(source_repo))


# ========================================================== 队列：领取语义
async def test_only_publishing_rows_are_claimable(source_repo):
    await make_run(source_repo, status=RunStatus.PENDING_APPROVAL)
    assert await runs_repo.claim_next_publishing("w1") is None

    row = await make_run(source_repo)
    claimed = await runs_repo.claim_next_publishing("w1")
    assert claimed.id == row.id


async def test_approval_releases_the_lease_so_publishing_starts_immediately(source_repo):
    """批准的同时清空租约。不清的话 publisher 要干等一个租约周期才接手。"""
    row = await make_run(source_repo)
    assert row.locked_by is None
    assert row.lease_expires_at is None
    assert await runs_repo.claim_next_publishing("w1") is not None


async def test_a_claimed_row_is_not_handed_to_a_second_publisher(source_repo):
    await make_run(source_repo)
    assert await runs_repo.claim_next_publishing("w1") is not None
    assert await runs_repo.claim_next_publishing("w2") is None


async def test_claiming_does_not_burn_an_agent_attempt(source_repo):
    """attempts 是 Agent 执行的预算，发布不该消耗它 —— 两个计数器别混用。"""
    row = await make_run(source_repo)
    claimed = await runs_repo.claim_next_publishing("w1")
    assert claimed.attempts == row.attempts


# ============================================== Worker.publish_once 端到端
async def test_publish_once_drives_the_run_to_published(source_repo, publisher, settings):
    row = await make_run(source_repo)
    worker = Worker(settings, EventBus(), worker_id="w1", publisher=publisher)

    assert await worker.publish_once() is True

    final = await runs_repo.get_run(row.id)
    assert final.status == RunStatus.PUBLISHED
    assert final.pr_url == f"https://github.com/{REPO}/pull/7"
    assert final.branch == branch_name(row.id)
    assert final.finished_at is not None
    assert final.locked_by is None  # 干完活要交还租约


async def test_publish_once_marks_failed_on_publish_error(source_repo, publisher, settings):
    row = await make_run(source_repo, diff="")
    worker = Worker(settings, EventBus(), worker_id="w1", publisher=publisher)

    assert await worker.publish_once() is True
    final = await runs_repo.get_run(row.id)
    assert final.status == RunStatus.FAILED
    assert "发布失败" in final.error


async def test_publish_once_is_a_noop_on_an_empty_queue(settings, publisher):
    worker = Worker(settings, EventBus(), worker_id="w1", publisher=publisher)
    assert await worker.publish_once() is False


# ================================================== 空转发布器（无 token）
async def test_dry_run_publisher_closes_the_loop_without_a_pr(source_repo, settings):
    """没配 token 时链路仍然走到 published，但 pr_url 是空的 —— 不能假装开了 PR。"""
    row = await make_run(source_repo)
    dry = DryRunPublisher()
    worker = Worker(settings, EventBus(), worker_id="w1", publisher=dry)

    await worker.publish_once()

    final = await runs_repo.get_run(row.id)
    assert final.status == RunStatus.PUBLISHED
    assert final.pr_url is None
    assert [r.id for r in dry.published] == [row.id]


def test_build_publisher_falls_back_to_dry_run_without_a_token(settings, monkeypatch):
    from repopilot.publishing import build_publisher

    monkeypatch.setattr(settings, "github_token", "")
    assert isinstance(build_publisher(settings), DryRunPublisher)
