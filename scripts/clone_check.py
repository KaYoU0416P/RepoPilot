"""真的 clone 一个公开仓库，验五件事。

    uv run python scripts/clone_check.py                      # 默认 octocat/Hello-World
    uv run python scripts/clone_check.py --repo psf/requests

**为什么要有这个脚本**：单元测试里的"远端"是本机一个裸仓库，走的是 git 的
`file://` 路径 —— 认证、HTTPS、仓库不存在时的报错，那条路一条都没覆盖到。
这个脚本补的就是"真的对着 github.com 跑一次"。

验的五件事：
  1. clone 得下来，缓存路径是仓库名的确定性函数
  2. **PAT 没有落进 `.git/config`** —— 缓存是长期目录，token 落盘就是明文存密钥
  3. 第二次是复用 + fetch，不是重新 clone
  4. clone 下来的仓库**接得上** WorkspaceManager（`.repos/` 和 `.workspaces/`
     的接缝：两边各自测过不代表接得上）
  5. 仓库不存在时报错清楚，且**不在缓存里留下半份仓库**
"""

import argparse
import asyncio
import shutil
import time
from pathlib import Path

from repopilot.config import Settings
from repopilot.observability import setup_logging
from repopilot.workspace.repos import RepoCache, RepoError

#: ★第一版这里塞了个假 token「反正公开仓库不需要认证」—— 然后 clone 直接失败：
#:
#:     remote: Invalid username or token. Password authentication is not supported
#:
#: 只要发了 `Authorization` 头，GitHub 就按那个身份判，**不会因为仓库是公开的
#: 就退回匿名访问**。也就是说一个过期的 PAT 会让**所有** clone 一起挂，
#: 包括公开仓库。**发凭证不是免费的：错的凭证比不发凭证更糟。**
#:
#: 所以这个脚本用**当前配置里真实的 token**（通常是空）跑真实 clone；
#: 「token 不落盘」那条在没有 token 时由单元测试兜底（test_repo_cache.py）。


async def main() -> int:
    parser = argparse.ArgumentParser(description="clone 链路自检")
    parser.add_argument("--repo", default="octocat/Hello-World")
    parser.add_argument("--keep", action="store_true", help="跑完不删缓存")
    args = parser.parse_args()

    setup_logging()
    settings = Settings()
    cache = RepoCache(settings)
    token = settings.github_token
    failures: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(f"  {'✓' if ok else '✗'} {label}")
        if not ok:
            failures.append(label)

    shutil.rmtree(cache.path_for(args.repo), ignore_errors=True)

    print(f"\n{'=' * 70}\nclone {cache.clone_url(args.repo)}\n{'=' * 70}")
    started = time.perf_counter()
    path = await cache.ensure(args.repo)
    first_ms = (time.perf_counter() - started) * 1000
    print(f"  → {path}  ({first_ms:.0f}ms)")
    print(f"  文件: {sorted(p.name for p in path.iterdir())[:8]}")

    check(path == cache.path_for(args.repo), "缓存路径 = path_for() 算出来的那个")

    config = (path / ".git" / "config").read_text(encoding="utf-8")
    check("x-access-token" not in config, "remote url 是干净的（token 没拼进 URL）")
    if token:
        check(token not in config, "PAT 没有落进 .git/config")
    else:
        print("  · 没配 token，「PAT 不落盘」由单元测试覆盖（见 test_repo_cache.py）")

    started = time.perf_counter()
    again = await cache.ensure(args.repo)
    second_ms = (time.perf_counter() - started) * 1000
    check(again == path, "第二次落在同一个目录（复用，不是每次新建）")
    print(f"  第二次 {second_ms:.0f}ms（首次 {first_ms:.0f}ms）")

    # 最后一环：clone 下来的仓库真的能变成一个 workspace 吗。
    # 这是 `.repos/` 和 `.workspaces/` 的接缝，两边各自测过不代表接得上。
    import tempfile

    from repopilot.workspace import WorkspaceManager

    with tempfile.TemporaryDirectory(prefix="clone-check-ws-") as tmp:
        manager = WorkspaceManager(Path(tmp))
        ws = manager.create(path, run_id="clone-check")
        check((ws.root / ".git").is_dir(), "workspace 有自己全新的 .git（基线提交建的）")
        check(
            not (ws.root / ".git" / "config").read_text(encoding="utf-8").count("origin"),
            "★上游的 .git/ 没被拷过来（否则 remote、凭证缓存全跟着进 workspace）",
        )
        check(len(ws.iter_files()) > 0, "文件树非空")
        manager.cleanup(ws)

    missing = f"{args.repo.split('/')[0]}/definitely-not-a-real-repo-xyz"
    try:
        await cache.ensure(missing)
        check(False, "不存在的仓库应该抛 RepoError")
    except RepoError as exc:
        print(f"  不存在的仓库 → {str(exc)[:120]}")
        check(not cache.is_cached(missing), "失败之后缓存里没留下半份仓库")

    if not args.keep:
        shutil.rmtree(cache.path_for(args.repo), ignore_errors=True)

    print(f"\n{len(failures)} 项不通过" if failures else "\n全部通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
