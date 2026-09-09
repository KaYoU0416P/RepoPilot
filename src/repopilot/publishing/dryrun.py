"""空转发布器：只记录「本来会做什么」，什么都不发出去。

存在的理由和 `ScriptedLLM` 一样 —— 让整条链路在**没有 token、不联网**的情况下
也能跑完，`make demo` 和测试才不需要一个真的 GitHub 仓库。

> 和 ScriptedLLM 一样，这东西证明的是**流水线通了**，不证明 PR 真的开得出来。
> 面试要主动区分这两件事。
"""

from repopilot.db.models import RunRow
from repopilot.observability import get_logger
from repopilot.publishing.base import PublishResult
from repopilot.publishing.github import branch_name

log = get_logger(__name__)


class DryRunPublisher:
    """`Publisher` 协议的空实现。"""

    def __init__(self, reason: str = "未配置 GITHUB_TOKEN") -> None:
        self.reason = reason
        #: 发布过哪些 run。测试断言用，也方便本地看链路有没有走到这一步。
        self.published: list[RunRow] = []

    async def publish(self, row: RunRow) -> PublishResult:
        branch = branch_name(row.id)
        self.published.append(row)
        log.info("[dry-run] run=%s 本应推分支 %s 并开 PR（%s）", row.id, branch, self.reason)
        return PublishResult(
            branch=branch,
            pr_url=None,  # 留空 = 没有真的 PR，别让人误会
            dry_run=True,
            detail=f"空转发布：{self.reason}",
        )
