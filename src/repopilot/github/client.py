"""GitHub REST API 客户端，只做我们真正要用的四件事。

刻意不用 PyGithub 之类的 SDK：
  - SDK 是同步的，塞进 asyncio 里要么阻塞事件循环，要么套线程池
  - 我们只用到 4 个端点，一层薄封装比一个几万行的依赖更好交代

认证用 **PAT**（Personal Access Token），不碰 OAuth。OAuth 要处理回调、
刷新、多租户存储，对一个后端服务的自动化场景是纯粹的负担。
"""

from typing import Any

import httpx

from repopilot.observability import get_logger

log = get_logger(__name__)

#: 锁死 API 版本。不锁的话 GitHub 改默认版本时你的服务会在某天早上莫名其妙地坏掉。
API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    """GitHub 返回了非 2xx。带上状态码和响应体，别只抛一句 'request failed'。"""

    def __init__(self, status_code: int, body: str, url: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitHub {status_code} {url}: {body[:300]}")


class GitHubClient:
    """一个 PAT + 一个 httpx.AsyncClient。

    `client` 参数是为了测试注入 `httpx.MockTransport` —— 测试不联网、不要真 token。
    这和 llm/ 里 ScriptedLLM 的思路一致：把外部依赖收敛到一个可替换的边界上。
    """

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------ 内部
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._api_url}{path}"
        response = await self._client.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            # 注意日志里只有 url，没有 headers —— token 绝不能进日志。
            log.warning("GitHub %s %s -> %s", method, path, response.status_code)
            raise GitHubError(response.status_code, response.text, url)
        return response.json() if response.content else None

    # ------------------------------------------------------------ 端点
    async def get_default_branch(self, repo: str) -> str:
        """PR 的 base 分支。别写死 "main" —— 老仓库还有一大堆是 master。"""
        data = await self._request("GET", f"/repos/{repo}")
        return data.get("default_branch") or "main"

    async def find_pull_request(self, repo: str, *, head_branch: str) -> dict | None:
        """这个分支上已经有开着的 PR 了吗？

        **这是发布幂等的核心。** publisher 在「推完分支」和「开完 PR」之间崩溃，
        重试时会走到这里；分支名是确定性的，所以能查到上一次开的 PR，
        直接复用而不是开第二个。

        owner 从 repo 里切出来：GitHub 的 head 过滤参数格式是 "owner:branch"。
        """
        owner = repo.split("/", 1)[0]
        data = await self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "open"},
        )
        return data[0] if data else None

    async def create_pull_request(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> dict:
        return await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
        )

    async def comment_on_issue(self, repo: str, issue_number: int, body: str) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
        )

    # -------------------------------------------------------- 生命周期
    async def aclose(self) -> None:
        """只关自己建的连接；注入进来的由调用方负责。"""
        if self._owns_client:
            await self._client.aclose()
