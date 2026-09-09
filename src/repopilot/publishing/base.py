"""发布的契约。

和 `llm/base.py` 同一个套路：先定协议，再给两个实现（真的 / 假的）。
好处是 worker 只依赖协议，测试和无 token 的本地环境用假的那个，
生产用真的，**worker 的代码一行都不用改**。
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from repopilot.db.models import RunRow


class PublishError(RuntimeError):
    """发布失败且**不该自动重试** —— 比如 diff 打不上、仓库不存在、token 没权限。

    和「进程崩了」严格区分：崩了会因为租约过期被自动重试（安全，因为发布是
    幂等的）；而这个异常代表逻辑上就是不行，重试多少次都一样，直接标 failed
    让人来看。等价于 MQ 里「进死信队列」和「重新投递」的区别。
    """


class PublishResult(BaseModel):
    branch: str
    pr_url: str | None = None
    comment_url: str | None = None
    #: 走的是空转实现（没配 token / 不是 GitHub 来源）。
    #: 落库时 pr_url 为空就代表这次没真的发出去，别让人误以为 PR 开好了。
    dry_run: bool = False
    detail: str = ""


@runtime_checkable
class Publisher(Protocol):
    """把一个已批准的 run 发布出去。

    Protocol = 结构化子类型：任何有这个方法签名的类都算实现，不用继承。
    Java 对照：接口，但**不需要 implements 声明** —— 更像 Go 的 interface。
    """

    async def publish(self, row: RunRow) -> PublishResult: ...
