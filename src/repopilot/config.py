"""Central settings. Reads env vars prefixed with REPOPILOT_ (plus ANTHROPIC_API_KEY)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator
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
    #: ★两个 key 都用 `AliasChoices` 同时认**带前缀**和**裸**两种写法。
    #:
    #: 为什么必须这样：`env_prefix` 只作用于**字段名推导出来的**变量名，
    #: 所以光有前缀的话，`.env` 里写 `ANTHROPIC_API_KEY=...` 会被**静默忽略** ——
    #: 不报错、不警告，只是降级成 scripted，然后你对着一份全 0 的报表发呆。
    #: 而裸名字恰恰是官方文档教你写的那个，`.env.example` 里也一直是裸的。
    #:
    #: 之前用 `os.environ.get()` 兜底，那只能捞到**进程环境变量**，
    #: 捞不到 `.env` 文件 —— 两条来源只补了一条。`AliasChoices` 一次覆盖两条。
    anthropic_api_key: str = Field(
        "", validation_alias=AliasChoices("REPOPILOT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )
    deepseek_api_key: str = Field(
        "", validation_alias=AliasChoices("REPOPILOT_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY")
    )
    #: 默认走 beta 通道：`strict`（保证 tool_call 参数符合 JSON Schema）只在那里有。
    #: 换成 Qwen / Kimi / GLM 的兼容端点也是改这一项。
    deepseek_base_url: str = "https://api.deepseek.com/beta"
    #: 单次回复的输出上限。
    #:
    #: ★**思考模型要留出思考的预算**。4096 是照 Anthropic 非思考模式调的，
    #: 搬到常驻思考模式的 DeepSeek V4 上直接不够用：思考过程本身就烧 output
    #: token，`execute` 节点还要整文件重写。真实评测里撞到过一次
    #: `finish_reason=length` —— 表现不是报错，是**回复被截断成半个 JSON**，
    #: 于是既没有 tool_call 也没有能解析的正文，整个 run 死在 `analyze`。
    #: DeepSeek V4 的输出上限是 384K，16384 只是个宽松得多的护栏。
    max_tokens: int = 16384

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

    #: ★一个 run 的 token 上限，超了就熔断（`llm/budget.py`）。
    #:
    #: `max_retries` 是**次数**预算，拦不住「在次数以内烧掉任意多 token」——
    #: 真实评测里撞到过三次：无解的题上模型反复推理，一次调用就烧光 16,384 个
    #: 输出 token，产出为零。这一条补的就是那个洞。
    #:
    #: 默认值**是从实测分布推出来的，不是拍脑袋**：54 次真实 run 里，
    #: 中位 4,258 token、p90 9,009、最大 39,062。120K ≈ 最大值的 3 倍 ——
    #: 正常 run 一次都不会碰到，真跑飞了能兜住。
    max_run_tokens: int | None = 120_000
    #: 美元上限，**补充**而非主控：定价表查不到的模型成本是 `None`，
    #: 没法比大小，于是它在最需要的时候恰好失效。token 那条永远有效。
    #: 同样按实测定：最贵的一次 run 是 $0.0688（DeepSeek）。
    #: ⚠️ 这个值和模型强相关 —— 换成 Opus 同样的活会贵一个量级，记得跟着调。
    max_run_cost_usd: float | None = 1.00

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

    @field_validator("max_run_tokens", "max_run_cost_usd", mode="before")
    @classmethod
    def _blank_means_no_limit(cls, value):
        """`REPOPILOT_MAX_RUN_TOKENS=` 留空 = 不限。

        没有这个的话，留空会撞上 pydantic 的 `int_parsing` 报错 —— 而「留空」
        恰恰是想关掉限制的人第一个会试的写法（环境变量没有"不设置"这个中间态，
        你要么不写，要么写成空）。**报错不是坏事，报一个看不懂的错才是。**
        """
        return None if isinstance(value, str) and not value.strip() else value


#: 每个 provider 的默认模型。**只在用户没显式设 `REPOPILOT_MODEL` 时生效** ——
#: 否则「换了 provider 忘了换 model」会把 `claude-sonnet-4-6` 发给 DeepSeek，
#: 换回来一个看不懂的 400。
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "deepseek": "deepseek-v4-pro",
}

#: provider → 它的 key 字段名。裸 / 带前缀两种写法由字段上的
#: `AliasChoices` 负责，这里只关心「这个 provider 的 key 拿到了没有」。
_PROVIDER_KEY_FIELDS = {
    "anthropic": "anthropic_api_key",
    "deepseek": "deepseek_api_key",
}


@lru_cache
def get_settings() -> Settings:
    """Cached so the whole process shares one Settings instance."""
    s = Settings()

    # 没设过 model 就跟着 provider 走。`model_fields_set` 是 pydantic 记录的
    # 「这个字段是被显式赋过值，还是在吃默认值」—— 用它才能区分
    # 「用户就是要 claude-sonnet-4-6」和「用户压根没管」。
    if "model" not in s.model_fields_set:
        s.model = DEFAULT_MODELS.get(s.llm_provider, s.model)

    # 选了某个 provider 却没有它的 key → 降级成 scripted。
    # 不抛异常是刻意的：跑测试和 demo 的人不该被迫先去申请 key。
    field = _PROVIDER_KEY_FIELDS.get(s.llm_provider)
    if field is not None and not getattr(s, field):
        s.llm_provider = "scripted"
    return s
