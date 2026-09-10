"""Token 计量与成本估算。

**纯数据 + 纯函数**：不碰网络、不碰数据库。所以定价表和加法规则可以直接测，
不用真的烧 token。

放在 `llm/` 下面而不是 `evaluation/`，因为它描述的是「调 LLM 这件事的开销」，
`evaluation/` 只是它的消费者之一。
"""

from pydantic import BaseModel, Field

from repopilot.config import ModelPricing


class Usage(BaseModel):
    """一次或多次 LLM 调用的累计用量。

    四个 token 字段直接对应 Anthropic 响应里的 `response.usage`。
    **不要只记 input/output** —— 那样会漏掉缓存，而缓存正是省钱的地方：
    命中缓存的 token 算 `cache_read_input_tokens`，**不算** `input_tokens`。
    只看 `input_tokens` 会以为自己特别省，其实只是没把另外两个字段加进来。
    """

    #: 未命中缓存、按全价计费的输入 token。
    input_tokens: int = 0
    output_tokens: int = 0
    #: 写入缓存的 token（比全价贵一点，默认约 1.25 倍）。
    cache_creation_input_tokens: int = 0
    #: 从缓存读出的 token（约全价的 0.1 倍）。
    cache_read_input_tokens: int = 0
    #: 调了几次。平均成本要除它，而且「一个 run 调几次 LLM」本身就是个指标。
    calls: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        """累加。用 `+` 而不是就地改，是为了让 Usage 保持「一个值」的语义 ——
        值对象不会被别处偷偷改掉，并发时也不用担心。

        Java 对照：等同于给一个不可变值对象写 `plus()`，像 `BigDecimal.add`。
        """
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(
                self.cache_read_input_tokens + other.cache_read_input_tokens
            ),
            calls=self.calls + other.calls,
        )

    @property
    def total_input_tokens(self) -> int:
        """真实的输入总量 = 全价 + 写缓存 + 读缓存。

        ★这三个是**互斥**的三份，不是同一份的不同视角。想知道「这次 prompt 到底
        多大」，必须三个相加 —— 只看 `input_tokens` 会严重低估。
        """
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """读缓存的 token 占总输入的比例。0 就是缓存**完全没生效**。"""
        total = self.total_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0


class CostBreakdown(BaseModel):
    """一次成本估算的结果。

    `total_usd` 是 `None` 而不是 `0.0` 时，表示**算不出来**（模型不在定价表里）。
    这个区分是刻意的：把未知报成 0 会让一份成本报表看起来免费，
    而「免费」正是最不该被静默相信的数字。
    """

    model: str
    total_usd: float | None
    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_write_usd: float = 0.0
    cache_read_usd: float = 0.0


_PER_MTOK = 1_000_000


def estimate_cost(usage: Usage, model: str, pricing: ModelPricing | None) -> CostBreakdown:
    """按定价表估算成本。纯函数。

    叫 `estimate` 不叫 `calculate`：这是**估算**。真实账单还受批量折扣、
    不同缓存 TTL（1 小时 TTL 的写入是 2 倍不是 1.25 倍）等因素影响。
    名字诚实一点，报表里的数字才不会被当成对账依据。
    """
    if pricing is None:
        return CostBreakdown(model=model, total_usd=None)

    input_usd = usage.input_tokens / _PER_MTOK * pricing.input_per_mtok
    output_usd = usage.output_tokens / _PER_MTOK * pricing.output_per_mtok
    cache_write_usd = (
        usage.cache_creation_input_tokens
        / _PER_MTOK
        * pricing.input_per_mtok
        * pricing.cache_write_multiplier
    )
    cache_read_usd = (
        usage.cache_read_input_tokens
        / _PER_MTOK
        * pricing.input_per_mtok
        * pricing.cache_read_multiplier
    )
    return CostBreakdown(
        model=model,
        total_usd=input_usd + output_usd + cache_write_usd + cache_read_usd,
        input_usd=input_usd,
        output_usd=output_usd,
        cache_write_usd=cache_write_usd,
        cache_read_usd=cache_read_usd,
    )


class UsageReport(BaseModel):
    """用量 + 成本，一起塞进 RunEvaluation 和评测报表的那个结构。"""

    model: str = ""
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None

    @property
    def cost_per_call_usd(self) -> float | None:
        if self.cost_usd is None or not self.usage.calls:
            return None
        return self.cost_usd / self.usage.calls


def report_for(llm: object) -> UsageReport:
    """从一个 LLM 客户端实例收割累计用量并折算成本。

    客户端本身就是 per-run 的累加器（`build_llm()` 每次新建），所以「实例累计」
    就是「这个 run 的总量」。

    做成模块级函数而不是留在 `finish` 节点里，是因为**崩掉的 run 也得收割** ——
    而崩掉的 run 恰恰是最想知道花了多少钱的那种：它烧了 token 却什么都没换回来。
    只在 `finish` 收割的话，异常一冒出来这笔账就永远丢了。
    """
    from repopilot.config import get_settings

    model = getattr(llm, "model", "unknown") or "unknown"
    usage = getattr(llm, "usage", None) or Usage()
    cost = estimate_cost(usage, model, get_settings().pricing_for(model))
    return UsageReport(model=model, usage=usage, cost_usd=cost.total_usd)
