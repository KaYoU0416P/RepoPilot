"""Central settings. Reads env vars prefixed with REPOPILOT_ (plus ANTHROPIC_API_KEY)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelPricing(BaseModel):
    """一个模型的单价，美元 / 百万 token。

    单价**必须**可配：它会变，而且写死在代码里的价格过期之后，报表还会一脸
    自信地给你一个错数字。默认值是官方公开价（2026-05 口径），
    可以用环境变量 `REPOPILOT_MODEL_PRICES`（JSON）整体覆盖。
    """

    input_per_mtok: float
    output_per_mtok: float
    #: 写入缓存比全价贵。默认 5 分钟 TTL 的 1.25 倍；1 小时 TTL 是 2 倍。
    cache_write_multiplier: float = 1.25
    #: 读缓存约为全价的 0.1 倍 —— 缓存省钱就省在这。
    cache_read_multiplier: float = 0.1


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOPILOT_",
        env_file=".env",
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: Literal["anthropic", "deepseek", "scripted"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    #: 默认走 beta 通道：`strict`（保证 tool_call 参数符合 JSON Schema）只在那里有。
    #: 换成 Qwen / Kimi / GLM 的兼容端点也是改这一项。
    deepseek_base_url: str = "https://api.deepseek.com/beta"
    max_tokens: int = 4096

    #: 模型单价表。**查不到的模型返回的成本是 `None` 而不是 0** ——
    #: 把「不知道」报成「免费」是成本报表最容易骗到自己的地方。
    model_prices: dict[str, ModelPricing] = {
        "claude-sonnet-4-6": ModelPricing(input_per_mtok=3.00, output_per_mtok=15.00),
        "claude-opus-4-8": ModelPricing(input_per_mtok=5.00, output_per_mtok=25.00),
        "claude-opus-4-7": ModelPricing(input_per_mtok=5.00, output_per_mtok=25.00),
        "claude-opus-4-6": ModelPricing(input_per_mtok=5.00, output_per_mtok=25.00),
        "claude-haiku-4-5": ModelPricing(input_per_mtok=1.00, output_per_mtok=5.00),
        # DeepSeek，**谷时价**（见下方 ⚠️）。缓存是自动的、不收写入费，
        # 所以 `cache_write_multiplier=0`：`cache_creation_input_tokens` 恒为 0，
        # 这一项在成本公式里自然消失。
        # cache_read 倍率由官方公布的命中价反推：0.022/0.66、0.007/0.22。
        #
        # ⚠️ **DeepSeek 是峰谷定价，峰时段单价翻倍**（UTC 01:00–04:00 与
        #    06:00–10:00 的工作日 ≈ 北京时间 09:00–12:00 / 14:00–18:00）。
        #    `ModelPricing` 是平价表，表达不了随时钟变的单价 —— 要做对得在
        #    **记账那一刻**钉住单价，而不是在 `estimate_cost` 那一刻算，
        #    否则纯函数就变成了依赖时钟的函数。这里按谷价记，
        #    白天跑出来的成本会被**低估最多一半**。见 progress.md 已知缺口。
        "deepseek-v4-pro": ModelPricing(
            input_per_mtok=0.66,
            output_per_mtok=1.98,
            cache_write_multiplier=0.0,
            cache_read_multiplier=0.0333,
        ),
        "deepseek-v4-flash": ModelPricing(
            input_per_mtok=0.22,
            output_per_mtok=0.66,
            cache_write_multiplier=0.0,
            cache_read_multiplier=0.0318,
        ),
    }

    def pricing_for(self, model: str) -> ModelPricing | None:
        return self.model_prices.get(model)

    # --- Agent budget ---
    max_retries: int = 2
    max_files_per_edit: int = 5

    # --- Safety limits ---
    tool_timeout_seconds: float = 20.0
    test_timeout_seconds: float = 60.0
    max_concurrent_tools: int = 4
    max_file_bytes: int = 200_000

    # --- Workspace ---
    workspace_root: Path = PROJECT_ROOT / ".workspaces"
    sample_repo: Path = PROJECT_ROOT / "fixtures" / "sample_repo"

    # --- 数据库 ---
    database_url: str = "postgresql://repopilot:repopilot@localhost:5433/repopilot"
    db_pool_min: int = 2
    db_pool_max: int = 10

    # --- Worker / 队列 ---
    #: 同时最多跑几个 Agent。和工具级并发是乘的关系，别调太大。
    max_concurrent_runs: int = 2
    #: 租约时长。worker 崩了之后，任务要等这么久才会被别人接手。
    lease_seconds: int = 120
    #: 队列空转时的轮询间隔。
    poll_interval_seconds: float = 1.0
    #: 优雅停机最多等多久。
    shutdown_grace_seconds: float = 30.0
    #: 单个任务最多被领取几次（含首次）。
    max_attempts: int = 3
    #: 关掉后 API 只入队不执行，方便单独起 worker 进程。
    enable_worker: bool = True

    # --- 可观测（Stage D）---
    #: 默认关。开了之后每个 span 会以一坨 JSON 打到 **stderr**，
    #: 平时开发时噪音太大；`make trace` 会临时打开它。
    otel_enabled: bool = False
    otel_service_name: str = "repopilot"

    # --- GitHub（Stage B）---
    #: PAT。开 PR / 回写评论用，不碰 OAuth。
    github_token: str = ""
    #: webhook 共享密钥。**留空 = 拒绝所有 webhook**（fail closed），
    #: 不是"留空就跳过验签"。默认放行的开关是最典型的生产事故。
    github_webhook_secret: str = ""
    #: Issue 打上这个标签才算授权 Agent 动手。默认不响应任何 Issue。
    github_trigger_label: str = "repopilot"
    github_api_url: str = "https://api.github.com"
    #: push 的目标前缀，拼成 `<base>/<owner>/<repo>.git`。
    #: 测试里指向本地裸仓库，于是 clone/push 走的是真 git，只有 HTTP 被替换。
    git_remote_base: str = "https://github.com"
    #: clone / apply / push 的墙钟超时。比工具超时长：clone 可能真的要一会儿。
    publish_timeout_seconds: float = 120.0


#: 每个 provider 的默认模型。**只在用户没显式设 `REPOPILOT_MODEL` 时生效** ——
#: 否则「换了 provider 忘了换 model」会把 `claude-sonnet-4-6` 发给 DeepSeek，
#: 换回来一个看不懂的 400。
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "deepseek": "deepseek-v4-pro",
}

#: provider → (设置里的 key 字段, 裸环境变量名)。
#: 裸环境变量是给「照着官方文档 export 了一下」的人兜底的，
#: 官方文档不会教你写 `REPOPILOT_` 前缀。
_PROVIDER_KEYS = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "deepseek": ("deepseek_api_key", "DEEPSEEK_API_KEY"),
}


@lru_cache
def get_settings() -> Settings:
    """Cached so the whole process shares one Settings instance."""
    import os

    s = Settings()
    for field, env_var in _PROVIDER_KEYS.values():
        if not getattr(s, field):
            setattr(s, field, os.environ.get(env_var, ""))

    # 没设过 model 就跟着 provider 走。`model_fields_set` 是 pydantic 记录的
    # 「这个字段是被显式赋过值，还是在吃默认值」—— 用它才能区分
    # 「用户就是要 claude-sonnet-4-6」和「用户压根没管」。
    if "model" not in s.model_fields_set:
        s.model = DEFAULT_MODELS.get(s.llm_provider, s.model)

    # 选了某个 provider 却没有它的 key → 降级成 scripted。
    # 不抛异常是刻意的：跑测试和 demo 的人不该被迫先去申请 key。
    entry = _PROVIDER_KEYS.get(s.llm_provider)
    if entry is not None and not getattr(s, entry[0]):
        s.llm_provider = "scripted"
    return s
