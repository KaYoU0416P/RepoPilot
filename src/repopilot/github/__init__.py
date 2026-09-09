"""GitHub 接入层：webhook 验签、事件解析、REST 客户端。"""

from repopilot.github.client import GitHubClient, GitHubError
from repopilot.github.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    IssueTrigger,
    extract_issue_trigger,
    parse_external_ref,
    sign,
    verify_signature,
)

__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "GitHubClient",
    "GitHubError",
    "IssueTrigger",
    "extract_issue_trigger",
    "parse_external_ref",
    "sign",
    "verify_signature",
]
