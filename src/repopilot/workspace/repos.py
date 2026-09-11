"""把远端仓库变成本地一份 clone，供 `WorkspaceManager` 拷贝。

两层目录，职责完全不同，**不要混**：

    .repos/<owner>/<repo>     ← 本模块负责。上游的镜像，只读 + fetch，
                                 全局共享，Agent 永远碰不到它
    .workspaces/<run_id>/     ← WorkspaceManager 负责。每个 run 一份副本，
                                 Agent 在这里面改，跑完就删

**clone 为什么不放在 webhook 处理函数里**：GitHub 对 webhook 的响应有 10 秒
超时，超了它判这次投递失败并重投。clone 一个真实仓库动辄几十秒 —— 放进去
就变成「每次都超时 → 每次都重投 → 每次都 clone」。所以 webhook 只写下
「这个 run 的仓库将来会在哪」，真正的 clone 在 worker 领取任务时才做。

本模块的三个安全要点，都不是洁癖：

1. **`owner/repo` 来自 webhook payload，同时被拼成 URL 和文件系统路径。**
   不校验的话 `a/../../../etc` 就写出缓存根目录了。而且以 `-` 开头的名字
   会被 git 当成**选项**而不是参数（`--upload-pack=...` 是已知的 RCE 面）。
2. **PAT 绝不落盘、绝不进 argv。** 见 `_auth_env`。
3. **clone 先写临时目录再原子改名。** 半份 clone 留在缓存路径上，
   下次就会被当成「已经有了」直接拿去用。
"""

import asyncio
import base64
import re
import shutil
import uuid
from pathlib import Path

from repopilot.config import Settings
from repopilot.observability import get_logger, span
from repopilot.sandbox import run_command

log = get_logger(__name__)

#: GitHub 的 owner / repo 允许的字符。首字符单独限制，**就是为了挡掉 `-` 开头** ——
#: 那种名字会被 git 当成选项解析。`.` 开头也挡掉（`.` / `..` 是路径穿越）。
_NAME = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
_FULL_NAME_RE = re.compile(rf"^({_NAME})/({_NAME})$")

#: git 子进程的基础环境。
#:
#: `GIT_TERMINAL_PROMPT=0` 是**必须的**：仓库是私有的而我们没权限时，git 默认
#: 会弹交互式用户名/密码提示，在没有 tty 的 worker 里它会一直挂着，直到墙钟
#: 超时才被杀。设成 0 让它立刻失败，错误信息也清楚得多。
#: （发布链路 `publishing/github.py` 有同样一份 —— 那边还要额外钉死提交时间戳，
#: 两处的用途不同，没有合并。）
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class RepoError(RuntimeError):
    """仓库名不合法，或者 clone / fetch 失败。"""


def parse_full_name(full_name: str) -> tuple[str, str]:
    """`"owner/repo"` → `("owner", "repo")`，不合法就抛 `RepoError`。

    这是**唯一**的入口校验。下游拿到的两段都保证：没有 `/`、没有 `..`、
    不以 `-` 或 `.` 开头 —— 于是「拼成路径」和「拼成 URL」两件事都安全了。
    """
    match = _FULL_NAME_RE.match((full_name or "").strip())
    if match is None:
        raise RepoError(f"仓库名不合法（要 owner/repo）: {full_name!r}")
    owner, repo = match.groups()
    # GitHub 的 clone URL 习惯带 .git 后缀，但目录名不该带。
    return owner, repo.removesuffix(".git") or repo


def _auth_env(settings: Settings) -> dict[str, str]:
    """把 PAT 交给 git，但**既不落盘也不进 argv**。

    两条被否掉的常见写法，以及为什么：

    - `git clone https://x-access-token:<PAT>@github.com/o/r.git`
      → token 会被 git 原样写进 `.git/config` 的 remote url，**留在磁盘上**。
        缓存目录是长期存在的，等于把 PAT 明文存了下来。
    - `git -c http.extraheader=... clone ...`
      → 进程的 argv 是**全机器可见**的（`ps aux` 谁都能看）。

    `GIT_CONFIG_COUNT/KEY/VALUE`（git ≥ 2.31）把配置从**环境变量**传进去：
    只对这一个子进程生效，不落盘，而环境变量不在别人的 `ps` 输出里。

    Java 对照：同样是「密钥别拼进命令行」，等价于不要
    `java -Dpassword=xxx`，改用环境变量或挂载的 secret。
    """
    if not settings.github_token:
        return {}
    basic = base64.b64encode(f"x-access-token:{settings.github_token}".encode()).decode()
    base = settings.git_remote_base.rstrip("/") + "/"
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{base}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


class RepoCache:
    """`.repos/` 下的上游镜像。一个仓库一份，多个 run 共享。"""

    def __init__(self, settings: Settings, root: Path | None = None) -> None:
        self.settings = settings
        self.root = (root or settings.repo_cache_root).resolve()
        #: 一个仓库一把锁。两个 run 同时命中同一个仓库时，第二个等第一个
        #: fetch 完 —— 否则两个 `git reset --hard` 会在同一个工作区里打架。
        #: ⚠️ 只在**进程内**有效。多 worker 进程要的是文件锁，见 progress.md。
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- 纯函数
    def path_for(self, full_name: str) -> Path:
        """`"owner/repo"` → 缓存里的绝对路径。**不做任何 IO。**

        webhook 靠它在「还没 clone」的时候就把 `runs.repo_path` 填上 ——
        路径是仓库名的确定性函数，所以入队和执行两个时刻算出来的是同一个。
        """
        owner, repo = parse_full_name(full_name)
        path = (self.root / owner / repo).resolve()
        # 正则已经挡住了穿越，这里再确认一次。**安全检查便宜，重复一遍不亏。**
        if self.root != path and self.root not in path.parents:
            raise RepoError(f"缓存路径逃出了 {self.root}: {full_name!r}")
        return path

    def clone_url(self, full_name: str) -> str:
        owner, repo = parse_full_name(full_name)
        return f"{self.settings.git_remote_base.rstrip('/')}/{owner}/{repo}.git"

    def is_cached(self, full_name: str) -> bool:
        return (self.path_for(full_name) / ".git").is_dir()

    # ------------------------------------------------------------------ IO
    async def ensure(self, full_name: str) -> Path:
        """保证本地有一份**干净的、跟上游一致的** clone，返回它的路径。

        已经有了就 fetch + 硬重置；没有就 clone。两条路径的后置条件一样，
        调用方不需要知道走的是哪条。
        """
        path = self.path_for(full_name)
        lock = self._locks.setdefault(full_name, asyncio.Lock())
        # 不能写成 `async with lock, span(...)`：一个 `async with` 里的每一项都
        # 会按**异步**协议去调，而 `span` 是同步的 contextmanager，直接 TypeError。
        async with lock:
            cached = (path / ".git").is_dir()
            with span("repo.ensure", repo=full_name, cached=cached):
                if cached:
                    await self._refresh(full_name, path)
                else:
                    await self._clone(full_name, path)
                return path

    async def _clone(self, full_name: str, path: Path) -> None:
        """clone 到临时目录，成功了再原子改名到最终位置。

        ★直接 clone 到最终路径的话，中途断网/超时会在缓存里留下半份仓库，
        而 `is_cached` 只看 `.git` 在不在 —— 下一个 run 就会拿这份残缺的
        仓库去干活，还查不出原因。**「失败要留下干净现场」比「失败要报错」更重要。**
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
        url = self.clone_url(full_name)
        log.info("clone %s -> %s", url, path)
        try:
            await self._git(
                # `--` 把「这之后全是参数，不是选项」钉死。仓库名已经过正则，
                # 这是第二道；写库配置的人换个正则时它还在。
                ["git", "clone", "--quiet", "--no-tags", "--", url, str(staging)],
                cwd=path.parent,
                failure=f"clone {full_name} 失败",
            )
            # 目标位置上可能有上一次失败留下的残骸（有目录但没 `.git`，
            # 所以 `is_cached` 看不见它）。`rename` 到一个非空目录会失败，
            # 先清掉 —— 走到这里就说明那份残骸没有任何价值。
            shutil.rmtree(path, ignore_errors=True)
            staging.rename(path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def _refresh(self, full_name: str, path: Path) -> None:
        """fetch + 硬重置回上游。

        为什么要 `reset --hard` + `clean -fdx`：缓存是给 `shutil.copytree`
        当源头用的，**它必须是上游的忠实镜像**。任何残留（上一次手滑改过、
        构建产物）都会被原样拷进每一个 workspace，然后混进 Agent 的 diff。
        """
        await self._git(
            ["git", "fetch", "--quiet", "--prune", "origin"],
            cwd=path,
            failure=f"fetch {full_name} 失败",
        )
        # 上游默认分支可能改名（master → main），所以不写死，问 origin/HEAD。
        head = await self._git(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=path,
            failure=f"读不到 {full_name} 的默认分支",
        )
        await self._git(
            ["git", "reset", "--quiet", "--hard", head.strip()],
            cwd=path,
            failure=f"重置 {full_name} 失败",
        )
        await self._git(["git", "clean", "-qfdx"], cwd=path, failure="清理工作区失败")

    async def _git(self, command: list[str], *, cwd: Path, failure: str) -> str:
        result = await run_command(
            command,
            cwd=cwd,
            timeout=self.settings.clone_timeout_seconds,
            env={**GIT_ENV, **_auth_env(self.settings)},
        )
        if not result.ok:
            detail = (result.stderr or result.stdout or "").strip()
            # 仓库不存在 / 没权限时，git 的报错里会回显 URL。我们的 URL 里没有
            # token（见 `_auth_env`），所以可以原样透出 —— 这正是不把 token
            # 拼进 URL 换来的好处之一：**报错可以放心打印**。
            raise RepoError(f"{failure}: {detail[:500]}{self._auth_hint(detail)}")
        return result.stdout

    def _auth_hint(self, detail: str) -> str:
        """★实测踩到的坑：**token 配错了，公开仓库也 clone 不下来。**

        只要我们发了 `Authorization` 头，GitHub 就按这个身份判，
        **不会因为"这个仓库反正是公开的"而退回匿名访问**。于是一个过期的 PAT
        会让所有 clone 一起挂掉，报错却是 "Invalid username or token" ——
        看起来像仓库或网络的问题，没人会第一时间想到是 token 过期了。

        **发凭证不是免费的：错的凭证比不发凭证更糟。** 所以这里把这句话
        直接写进报错 —— 排查时间省在这一行上。
        """
        if not self.settings.github_token:
            return ""
        if "Invalid username or token" not in detail and "Authentication failed" not in detail:
            return ""
        return (
            "\n提示：REPOPILOT_GITHUB_TOKEN 可能已过期或无权限。"
            "注意**公开仓库也会因此失败** —— 带了 Authorization 头之后 "
            "GitHub 不会退回匿名访问。不需要认证时把这个变量留空。"
        )
