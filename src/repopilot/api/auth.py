"""API 鉴权：Bearer token + 权限位（scope）。

**刻意不做 OAuth / JWT。** 这个服务的调用方是 CI 机器人和少数几个人，
不是"任意第三方应用代表用户访问" —— 那才是 OAuth 要解决的问题。
用 OAuth 只会引入一个授权服务器、一堆回调、和一个我讲不清楚的登录流程。
**认证方案要配得上威胁模型，不是越重越好。**

三个设计要点，比代码本身值得讲：

1. **默认拒绝。** 鉴权挂在 `APIRouter(dependencies=...)` 上，不是逐个路由加。
   逐个加的失败形态是「新写了一个接口忘了加注解 → 它是公开的」——
   **一个安全控制如果靠人记得加，它迟早会漏。**
   公开的接口反过来要显式放进 `public` 那个 router，一眼就能数清楚有几个。
   Java 对照：Spring Security 写 `anyRequest().authenticated()` + 显式
   `permitAll()` 白名单，而不是满代码撒 `@PreAuthorize`。

2. **审批要单独的权限位。** 整个项目的核心是「Agent 说成功不算数，要人批准」。
   如果开 run 的那把 key 也能批准自己开的 run，**这道闸门就是装饰品**。
   CI 机器人拿 `run`，人拿 `run,approve`。

3. **`decided_by` 来自身份，不来自请求体。** 见 `routes.py::decide_approval`。
"""

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from repopilot.config import Settings, get_settings
from repopilot.observability import get_logger

log = get_logger(__name__)

SCHEME = "Bearer"

#: 401 的响应体。**对外统一，不区分「服务端没配 key」「key 不对」「格式不对」。**
#: 详细原因只进日志 —— 报错要对运维详细、对外部统一，否则 401 本身就成了
#: 一个探测接口（"哦，这台服务器压根没配 key"）。
_UNAUTHORIZED = "需要合法的 API key：Authorization: Bearer <key>"


@dataclass(frozen=True)
class Principal:
    """通过鉴权的调用方。**处理函数只该看见它，不该看见 key 本身。**"""

    name: str
    scopes: frozenset[str]

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def _extract_token(request: Request) -> str | None:
    """从 `Authorization: Bearer <token>` 里取 token。

    刻意不用 fastapi 的 `HTTPBearer`：它在缺 header 时抛 403，
    而正确答案是 **401 + `WWW-Authenticate`** —— 401 是"你没表明身份"，
    403 是"我知道你是谁，但你不能干这个"。两者混在一起会让调用方
    分不清该去拿 key 还是该去要权限。
    """
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != SCHEME.lower() or not token.strip():
        return None
    return token.strip()


def identify(token: str, settings: Settings) -> Principal | None:
    """token → Principal，对不上返回 None。

    ★**比较必须是常数时间，而且不能短路。**

    - 用 `==` 比 token：一发现某字节不同就返回，耗时泄露了"前几位对上了"，
      攻击者逐字节就能把 key 试出来（timing attack）。和 webhook 验签
      同一个坑，所以同样用 `hmac.compare_digest`。
    - 循环里 `if match: return`：**匹配到第几把 key 会从耗时里泄露出来**。
      泄露的信息比上一条弱得多，但代价只是少写一个 `return`，不值得省。
      所以这里把结果累加，跑完所有 key 才返回。

    Java 对照：`MessageDigest.isEqual`，绝不用 `String.equals` 比密钥。
    """
    found: Principal | None = None
    for configured in settings.api_keys:
        if hmac.compare_digest(configured.key.encode(), token.encode()):
            found = Principal(name=configured.name, scopes=frozenset(configured.scopes))
    return found


def require_scope(scope: str):
    """生成一个"要求某个权限位"的依赖。

    返回的是**函数**不是结果 —— FastAPI 的依赖是按可调用对象注册的，
    要给不同路由要求不同权限，就得有个工厂把 `scope` 闭包进去。
    （Python 里闭包是最轻的"带参数的策略对象"，Java 得写个类或 lambda 工厂。）
    """

    async def dependency(
        request: Request, settings: Settings = Depends(get_settings)
    ) -> Principal:
        # ★fail closed：一把 key 都没配 = 拒绝所有，不是"没配就放行"。
        # 默认放行的开关是最典型的生产事故：某次部署漏注入一个环境变量，
        # 接口就裸奔了，而且日志里一条报错都没有。
        # 和 webhook 验签的 `not secret → False` 是同一条规矩。
        if not settings.api_keys:
            log.error("没有配置 API key（REPOPILOT_API_KEYS），所有业务接口都会返回 401")
            raise _unauthorized()

        token = _extract_token(request)
        if token is None:
            log.warning("%s %s 缺少或格式不对的 Authorization 头", request.method, request.url.path)
            raise _unauthorized()

        principal = identify(token, settings)
        if principal is None:
            log.warning("%s %s 使用了未知的 API key", request.method, request.url.path)
            raise _unauthorized()

        if not principal.can(scope):
            # ★403 而不是 401：身份是认的，权限不够。换一把 key 重试才有意义，
            # 回 401 会让调用方以为 key 坏了，跑去重新签发一把同样没权限的。
            held = ", ".join(sorted(principal.scopes)) or "无"
            log.warning("%s 权限不足：需要 %r，它只有 %s", principal.name, scope, held)
            raise HTTPException(
                status_code=403,
                detail=f"这把 key 没有 {scope!r} 权限（它有：{held}）",
            )

        # 身份进日志上下文，审计才有意义：谁批准了哪个 run 必须查得出来。
        request.state.principal = principal
        return principal

    return dependency


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=_UNAUTHORIZED,
        # 少了这个头就不是一个合规的 401（RFC 9110）。客户端库靠它知道
        # 该用哪种认证方式重试。
        headers={"WWW-Authenticate": SCHEME},
    )
