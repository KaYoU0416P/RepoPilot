"""发布层：把批准过的 diff 变成一个 PR。"""

from repopilot.config import Settings
from repopilot.github import GitHubClient
from repopilot.observability import get_logger
from repopilot.publishing.base import Publisher, PublishError, PublishResult
from repopilot.publishing.dryrun import DryRunPublisher
from repopilot.publishing.github import GitHubPublisher, branch_name

log = get_logger(__name__)

__all__ = [
    "DryRunPublisher",
    "GitHubPublisher",
    "PublishError",
    "PublishResult",
    "Publisher",
    "branch_name",
    "build_publisher",
]


def build_publisher(settings: Settings) -> Publisher:
    """按配置挑一个实现。和 `llm/build_llm()` 是同一个模式。

    **没配 token 就降级成空转，而不是报错。** 理由：发布是链路的最后一段，
    没有 token 时前面所有环节（入队、Agent、审批）都还是有意义的，
    不该因为最后一段配置缺失就让整个服务起不来。

    这和 webhook 验签的 fail closed 看似矛盾，其实标准不一样：
    **验签是安全边界，缺配置必须拒绝；发布是功能，缺配置降级并且说清楚。**
    """
    if not settings.github_token:
        return DryRunPublisher()
    client = GitHubClient(settings.github_token, api_url=settings.github_api_url)
    return GitHubPublisher(settings, client)
