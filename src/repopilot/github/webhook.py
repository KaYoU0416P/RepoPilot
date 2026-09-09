"""GitHub webhook：验签 + 事件解析。

这个模块刻意**不碰数据库、不碰 FastAPI**。输入是 `bytes` 和 `dict`，输出是
布尔值和一个小 DTO。所以它能被当纯函数测试 —— 不用起服务、不用连库、
不用造 Request 对象。安全相关的代码越容易测越好。
"""

import hashlib
import hmac
from typing import Any

from pydantic import BaseModel

#: GitHub 投递时带的三个 header。名字写死在这里，别散落到路由里。
SIGNATURE_HEADER = "X-Hub-Signature-256"  # 签名，格式 "sha256=<hex>"
DELIVERY_HEADER = "X-GitHub-Delivery"  # 投递 UUID，重投时不变 → 幂等键
EVENT_HEADER = "X-GitHub-Event"  # 事件类型，如 "issues" / "ping"

SIGNATURE_PREFIX = "sha256="

#: 哪些 issue 动作值得看一眼。closed / deleted / assigned 之类直接忽略。
TRIGGER_ACTIONS = frozenset({"opened", "reopened", "labeled"})


def sign(secret: str, body: bytes) -> str:
    """按 GitHub 的规则算出签名头的值。

    生产代码不用它 —— 我们只验签不发签。它的用途是**测试里造合法请求**，
    以及本地 demo 时用 curl 手动模拟一次投递。
    """
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """校验 GitHub 的 X-Hub-Signature-256。

    契约（tests/test_webhook.py 是验收标准）：

      secret 为空          → False   fail closed，没配密钥就拒绝所有请求，
                                     绝不能"没配就跳过验签"
      header 为 None/空串  → False
      header 没有 sha256= 前缀 → False
      body 被改过一个字节  → False
      签名正确             → True

    要点一：算法是 HMAC-SHA256(key=secret, msg=body)，输出十六进制小写，
            前面拼 "sha256="。

    要点二：**比较必须用 `hmac.compare_digest`，不能用 `==`。**
            `==` 一发现不同就返回，耗时泄露了"前几位对上了"这个信息，
            攻击者可以逐字节把签名试出来。`compare_digest` 是常数时间。
            Java 里对应 `MessageDigest.isEqual`。

    要点三：body 必须是**原始字节**。反序列化再序列化 JSON 不是恒等操作
            （key 顺序、空格都会变），签名一定对不上。所以路由里是
            `await request.body()` 而不是 `body: dict`。
    """
    # ★核心 1/2：fail closed。
    # 密钥没配置 → 拒绝所有请求，而不是"没配就跳过验签"。
    # 默认放行的开关是最典型的生产事故：某次部署漏注入一个环境变量，
    # 接口就裸奔了，而且日志里一条报错都没有。
    # header 缺失/空串在这里一起挡掉，下面就不用再判 None。
    if not secret or not header:
        return False

    # 我们自己按同样的规则算一遍。expected 形如 "sha256=<64位小写hex>"，
    # 所以 header 里的 "sha256=" 前缀不用单独校验 —— 前缀不对，整串就不相等。
    expected = sign(secret, body)

    # ★核心 2/2：常数时间比较，绝对不能写成 `expected == header`。
    #
    # `==` 一发现某个字节不同就立刻返回，耗时因此泄露了"前面几位猜对了"。
    # 攻击者反复请求、测量响应时间，就能一个字节一个字节地把签名试出来
    # （timing attack / 旁路攻击）。
    # compare_digest 无论在第几位不同都走完全程，耗时不携带任何信息。
    #
    # Java 对照：MessageDigest.isEqual(byte[], byte[])。做支付回调验签、
    # 比对 API token 时用的是同一个东西，绝不用 String.equals。
    #
    # 这里 .encode() 不是多余的：compare_digest 传 str 时**要求纯 ASCII**，
    # 否则抛 TypeError。而 header 是攻击者完全可控的，塞一个中文进来就能把
    # 401 变成 500 —— 那是一个白送的 DoS / 信息泄露面。转成 bytes 就没这问题。
    return hmac.compare_digest(expected.encode(), header.encode())


class IssueTrigger(BaseModel):
    """一个"值得开 run"的 Issue，从 webhook payload 里抽出来的最小信息。

    刻意不保留整个 payload：payload 有几百个字段，绝大多数和我们无关。
    只留下游真正要用的，边界就清晰了。
    """

    repo_full_name: str  # "owner/repo"
    issue_number: int
    title: str
    body: str = ""
    labels: list[str] = []

    @property
    def external_ref(self) -> str:
        """写进 runs.external_ref 的值，形如 "owner/repo#42"。

        Stage B 第三步回写 Issue 评论时，靠它反查该评论到哪儿去。
        """
        return f"{self.repo_full_name}#{self.issue_number}"

    def to_task(self) -> str:
        """Issue 标题 + 正文 → 交给 Agent 的任务描述。"""
        return f"{self.title}\n\n{self.body}".strip()


def extract_issue_trigger(
    payload: dict[str, Any], *, trigger_label: str
) -> IssueTrigger | None:
    """判断这个 issues 事件该不该入队；不该就返回 None。

    四道过滤，任何一道不过就忽略：
      1. action 不在 TRIGGER_ACTIONS 里（closed / assigned / ... ）
      2. 没有 issue 字段（payload 结构不对）
      3. 这其实是个 PR —— GitHub 把 PR 也当 issue 发事件，
         payload["issue"] 里带 "pull_request" 键的就是 PR，必须排掉，
         否则我们会试图去"修"一个 PR
      4. 标签里没有 trigger_label

    第 4 条是最重要的授权边界：**默认不响应**。仓库里所有 Issue 都会推事件
    过来，只有人主动打上 repopilot 标签才算"授权 Agent 动这个仓库"。
    """
    if payload.get("action") not in TRIGGER_ACTIONS:
        return None

    issue = payload.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None

    labels = [
        label["name"]
        for label in issue.get("labels") or []
        if isinstance(label, dict) and "name" in label
    ]
    if trigger_label not in labels:
        return None

    repo = payload.get("repository") or {}
    return IssueTrigger(
        repo_full_name=repo.get("full_name") or "unknown/unknown",
        issue_number=issue.get("number") or 0,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        labels=labels,
    )
