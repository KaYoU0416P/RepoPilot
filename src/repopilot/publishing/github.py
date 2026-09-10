"""真正的发布：git 建分支 + push，然后开 PR、回写 Issue 评论。

发布这一步的设计要点，比代码本身更值得讲：

**1. 只依赖数据库里的 diff，不依赖磁盘上的 workspace。**
   Agent 跑完到人批准之间可能隔几小时，中间进程重启过、`.workspaces/` 被
   `make clean` 删过都很正常。所以发布是从 `repo_path` 重新 clone 一份，
   把 `runs.diff` 打上去 —— 整个过程可以在任何一台机器上重放。

**2. 分支名是确定性的，它就是幂等键。**
   `repopilot/run-<id前8位>`。push 同样的提交是 no-op；开 PR 之前先查
   「这个 head 分支上有没有开着的 PR」。所以 publisher 在任何一步崩掉，
   重试都不会开出第二个 PR。

**3. 失败分两类。** 逻辑失败（diff 打不上、没权限）抛 `PublishError` → 标 failed，
   不重试；进程崩溃 → 租约过期 → 自动重试，靠上面第 2 点保证安全。
"""

import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from repopilot.config import Settings
from repopilot.db.models import RunRow
from repopilot.github import GitHubClient, GitHubError, parse_external_ref
from repopilot.observability import get_logger, mark_error, set_attrs, span
from repopilot.publishing.base import PublishError, PublishResult
from repopilot.sandbox import run_command

log = get_logger(__name__)

#: git 子进程的环境变量。
#:
#: GIT_TERMINAL_PROMPT=0 是**必须的**：token 过期时 git 默认会弹出交互式的
#: 用户名/密码提示，在一个没有 tty 的后台进程里它会一直挂着，直到我们的
#: 墙钟超时才被杀掉。设成 0 让它立刻失败，错误信息也清楚得多。
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",  # 兜底：真有人问密码，就回一个空串
    "GIT_CONFIG_NOSYSTEM": "1",  # 别读机器上的全局 git 配置，行为要可复现
}

COMMITTER = ["-c", "user.name=repopilot", "-c", "user.email=agent@repopilot.local"]


def commit_env(row: RunRow) -> dict[str, str]:
    """让同一个 run 每次都提交出**完全一样的 commit SHA**。

    ★这是"重复 push 是 no-op"能成立的前提，一开始我漏了，测试抓到了。

    git 的 commit SHA 是对「树 + 父提交 + 作者 + 提交者 + **时间戳** + 消息」
    整体做哈希。树和父提交本来就一样（同一份 clone + 同一份 diff），但时间戳
    默认取当下 —— 于是重试时算出来的是另一个 SHA，push 上去就变成
    non-fast-forward 被远端拒绝，"幂等"直接失效。

    把两个日期钉死在 `run.created_at` 上，SHA 就完全确定了：第二次 push
    推的是和远端**一模一样**的 commit，git 直接返回 Everything up-to-date。

    （这个 bug 表现为"偶尔失败"：两次发布落在同一秒时 SHA 恰好相同就过了。
    最初那版测试就是这么飘的。时间相关的不确定性一定要钉死，不能靠运气。）
    """
    stamp = row.created_at.isoformat()
    return {**GIT_ENV, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def branch_name(run_id) -> str:
    """确定性分支名 —— 同一个 run 永远算出同一个分支。见模块头注释第 2 点。"""
    return f"repopilot/run-{str(run_id)[:8]}"


def strip_diff_preamble(diff: str) -> str:
    """把 `git diff --stat --patch` 前面那段 diffstat 摘要切掉。

    `git_diff` 工具为了让 LLM 看得懂，输出里带了 `--stat` 的统计摘要。
    `git apply` 大多数时候能自己跳过前言，但那是「大多数时候」——
    这种地方不要赌，显式从第一行 `diff --git` 开始截。
    """
    index = diff.find("diff --git ")
    return diff[index:] if index > 0 else diff


class GitHubPublisher:
    """`Publisher` 协议的真实实现。"""

    def __init__(
        self,
        settings: Settings,
        client: GitHubClient,
        *,
        remote_base: str | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        # 测试里指向一个本地裸仓库，于是 clone/push 全是**真的 git**，
        # 只有 HTTP 那半边被替换掉。比整个 mock 掉 git 有价值得多。
        self.remote_base = (remote_base or settings.git_remote_base).rstrip("/")

    # --------------------------------------------------------------- 主流程
    async def publish(self, row: RunRow) -> PublishResult:
        ref = parse_external_ref(row.external_ref)
        if ref is None:
            raise PublishError(f"external_ref 不是 owner/repo#n 格式: {row.external_ref!r}")
        repo, issue_number = ref

        if not (row.diff or "").strip():
            raise PublishError("这个 run 没有 diff，没有东西可发布")

        branch = branch_name(row.id)
        workdir = Path(tempfile.mkdtemp(prefix=f"publish-{str(row.id)[:8]}-"))
        # 发布是**另一条 trace**，不是 run 那条的延续：中间隔着人工审批，
        # 可能是几小时后、另一个进程、甚至另一台机器。硬把两者串成一条 trace
        # 只会得到一个跨度几小时、中间全是空白的 span。
        # 它们靠 `run_id` 这个属性关联 —— 这正是 run_id 值得冗余写进每个 span 的原因。
        with span("publish", repo=repo, issue=issue_number, branch=branch) as current:
            try:
                await self._prepare_branch(workdir, row, branch)
                await self._push(workdir, repo, branch)
                result = await self._open_pr_and_comment(row, repo, issue_number, branch)
                set_attrs(current, pr_url=result.pr_url, detail=result.detail)
                return result
            finally:
                # 发布产物已经在远端了，本地这份临时目录没有任何保留价值。
                shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------ git 那半边
    async def _prepare_branch(self, workdir: Path, row: RunRow, branch: str) -> None:
        """clone → 建分支 → 打补丁 → 提交。"""
        clone = workdir / "repo"
        await self._git(
            # --no-hardlinks：默认的硬链接会让两个仓库共享 object 文件，
            # 我们等下要在这里面乱改，不能有任何回流到源仓库的可能。
            ["git", "clone", "--no-hardlinks", "--quiet", str(row.repo_path), str(clone)],
            cwd=workdir,
            failure="clone 目标仓库失败",
        )
        await self._git(["git", "checkout", "-q", "-b", branch], cwd=clone, failure="建分支失败")

        # 补丁文件放在 clone **外面**：放里面的话下面 `git add -A` 会把它一起提交进去。
        patch = workdir / "run.patch"
        patch.write_text(strip_diff_preamble(row.diff or ""), encoding="utf-8")
        await self._git(
            ["git", "apply", "--whitespace=nowarn", str(patch)],
            cwd=clone,
            failure="diff 打不上去（源仓库可能在 Agent 跑完之后又变了）",
        )

        await self._git(["git", "add", "-A"], cwd=clone, failure="git add 失败")
        await self._git(
            ["git", *COMMITTER, "commit", "-q", "--no-gpg-sign", "-m", self._commit_message(row)],
            cwd=clone,
            failure="提交失败",
            env=commit_env(row),  # ← 时间戳钉死，SHA 才确定
        )

    async def _push(self, workdir: Path, repo: str, branch: str) -> None:
        """推分支。重复推同样的提交是 no-op，所以这一步天然幂等。"""
        await self._git(
            ["git", "push", "--quiet", self._remote_url(repo), f"HEAD:refs/heads/{branch}"],
            cwd=workdir / "repo",
            failure="推送分支失败（检查 token 权限）",
            # 报错信息里可能带 remote URL，而 URL 里有 token。绝不能原样抛出去。
            redact=self._token_values(),
        )

    def _remote_url(self, repo: str) -> str:
        """把 PAT 塞进 https URL 的 userinfo 里。

        `x-access-token:<PAT>@host` 是 GitHub 官方给自动化用的写法。
        本地路径（测试用的裸仓库）原样返回，不做任何拼接。
        """
        base = f"{self.remote_base}/{repo}.git"
        if not base.startswith("https://") or not self.settings.github_token:
            return base
        parts = urlparse(base)
        netloc = f"x-access-token:{self.settings.github_token}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunparse(parts._replace(netloc=netloc))

    def _token_values(self) -> list[str]:
        return [t for t in (self.settings.github_token,) if t]

    # ----------------------------------------------------------- API 那半边
    async def _open_pr_and_comment(
        self, row: RunRow, repo: str, issue_number: int, branch: str
    ) -> PublishResult:
        try:
            with span("github.pull_request", repo=repo, branch=branch) as current:
                # ★幂等：先看这个分支上是不是已经开过 PR 了。
                # 上一次在「push 成功、开 PR 之前」崩掉的话，这里就能捡回来。
                existing = await self.client.find_pull_request(repo, head_branch=branch)
                if existing is not None:
                    log.info("run=%s 分支 %s 上已有 PR，复用不重开", row.id, branch)
                    pr = existing
                else:
                    base = await self.client.get_default_branch(repo)
                    pr = await self.client.create_pull_request(
                        repo,
                        head=branch,
                        base=base,
                        title=self._pr_title(row, issue_number),
                        body=self._pr_body(row, issue_number),
                    )
                # 幂等路径被走中的次数，是"崩溃重试到底安不安全"唯一的现场证据。
                set_attrs(current, reused=existing is not None)

                pr_url = pr.get("html_url")
                comment = await self.client.comment_on_issue(
                    repo, issue_number, f"RepoPilot 已提交修复：{pr_url}"
                )
        except GitHubError as exc:
            # 4xx 是我们自己的问题（权限/参数），重试没意义 → 不重试。
            # 5xx / 429 是对方的问题，值得重试 → 让它作为普通异常冒出去，
            # 由租约机制在下一轮重新领取。
            if 400 <= exc.status_code < 500 and exc.status_code != 429:
                raise PublishError(str(exc)) from exc
            raise

        return PublishResult(
            branch=branch,
            pr_url=pr_url,
            comment_url=comment.get("html_url") if comment else None,
            detail="已复用已存在的 PR" if existing is not None else "已创建 PR",
        )

    # ------------------------------------------------------------------ 文案
    @staticmethod
    def _commit_message(row: RunRow) -> str:
        headline = (row.task or "自动修复").splitlines()[0][:72]
        return f"fix: {headline}\n\nRepoPilot run {row.id}"

    @staticmethod
    def _pr_title(row: RunRow, issue_number: int) -> str:
        headline = (row.task or "自动修复").splitlines()[0][:72]
        return f"[RepoPilot] {headline} (#{issue_number})"

    @staticmethod
    def _pr_body(row: RunRow, issue_number: int) -> str:
        """PR 正文要写清楚「这是机器改的、人批准过」—— review 的人有权知道。"""
        files = "\n".join(f"- `{f}`" for f in row.files_changed) or "- (无)"
        return (
            f"由 RepoPilot 自动生成，关联 #{issue_number}。\n\n"
            f"**改动文件**\n{files}\n\n"
            f"**重试次数**：{row.retry_count}\n\n"
            f"**Agent 报告**\n\n{row.final_report or '(无)'}\n\n"
            f"---\n"
            f"该改动已通过人工审批闸门（run `{row.id}`），但仍需 review。\n"
        )

    # ------------------------------------------------------------------ 内部
    async def _git(
        self,
        command: list[str],
        *,
        cwd: Path,
        failure: str,
        redact: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """跑一条 git 命令，失败就抛 PublishError。

        走 `sandbox.run_command` 而不是 `subprocess`：白拿墙钟超时和进程组
        kill —— 一个卡在网络上的 git push 不能把 worker 拖住。
        """
        # ★span 名字只取子命令（clone / push / apply…），**绝不把 command 整个
        # 写进属性**：`git push` 的参数里带着 remote URL，而 URL 的 userinfo 里
        # 塞着 PAT。日志那边已经有 `redact` 在兜底，trace 是同一类外泄通道，
        # 属性里放什么必须一样谨慎 —— span 属性是明文，而且会被导出到别人家。
        with span(f"git.{command[1]}", cwd=cwd.name) as current:
            result = await run_command(
                command, cwd=cwd, timeout=self.settings.publish_timeout_seconds, env=env or GIT_ENV
            )
            if not result.ok:
                detail = (result.stderr or result.stdout or "").strip()
                for secret in redact or []:
                    detail = detail.replace(secret, "***")
                mark_error(current, failure)
                raise PublishError(f"{failure}: {detail[:500]}")
