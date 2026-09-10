"""成本熔断。不联网不花钱。

熔断这种东西，「能掐」是最容易测对的一半；难的是边界：
掐早了正常 run 会挂，掐晚了等于没有，算不出成本时不能默认放行也不能默认掐死。
"""

import pytest

from repopilot.agent.nodes import Nodes
from repopilot.agent.schemas import Analysis
from repopilot.agent.state import initial_state
from repopilot.config import Settings, get_settings
from repopilot.llm import BudgetExceeded, build_llm
from repopilot.llm.base import LLMError
from repopilot.llm.budget import BudgetedLLM, RunBudget
from repopilot.llm.usage import Usage, UsageReport


def _report(tokens: int = 0, cost: float | None = None, model: str = "m") -> UsageReport:
    return UsageReport(model=model, usage=Usage(input_tokens=tokens), cost_usd=cost)


class _FakeLLM:
    """只累加用量、从不真的调用的假客户端。"""

    name = "fake"
    model = "deepseek-v4-pro"

    def __init__(self, per_call: int = 1_000) -> None:
        self.usage = Usage()
        self.calls = 0
        self._per_call = per_call

    async def structured(self, *, system, user, schema):
        self.calls += 1
        self.usage = self.usage + Usage(input_tokens=self._per_call, calls=1)
        return schema(reasoning="ok")


# ============================================================== 判据本身
def test_no_limits_never_breaches():
    """两条都是 None = 不限。默认关掉时不能有任何行为变化。"""
    assert RunBudget().breach(_report(tokens=10**9, cost=10**6)) is None


def test_token_limit_trips_at_the_threshold():
    budget = RunBudget(max_tokens=1_000)
    assert budget.breach(_report(tokens=999)) is None
    assert budget.breach(_report(tokens=1_000)) is not None


def test_the_reason_says_which_limit_and_how_much():
    """★返回字符串不是 bool：「掐了」和「掐的是哪条、当时烧到多少」是两件事。"""
    reason = RunBudget(max_tokens=1_000).breach(_report(tokens=1_500))
    assert "token" in reason
    assert "1,500" in reason and "1,000" in reason


def test_cost_limit_trips_too():
    budget = RunBudget(max_cost_usd=0.50)
    assert budget.breach(_report(cost=0.49)) is None
    assert "成本" in budget.breach(_report(cost=0.50))


def test_unknown_cost_does_not_trip_the_dollar_limit():
    """★成本是 `None`（定价表里没这个模型）时，美元那条**不参与判断**。

    这是 `estimate_cost` 那条「未知不等于 0」的下游后果：`None` 没法比大小。
    把「算不出来」当成「超了」会让任何没登记的模型直接跑不动；
    当成「没超」才对 —— 因为主控是 token 那条，它永远算得出来。
    """
    assert RunBudget(max_cost_usd=0.01).breach(_report(cost=None)) is None


def test_token_limit_still_works_when_cost_is_unknown():
    """接上一条：美元失效的场合，正是 token 兜底的场合。"""
    budget = RunBudget(max_tokens=100, max_cost_usd=0.01)
    assert budget.breach(_report(tokens=200, cost=None)) is not None


def test_either_limit_alone_is_enough_to_trip():
    budget = RunBudget(max_tokens=10**9, max_cost_usd=0.01)
    assert budget.breach(_report(tokens=1, cost=0.02)) is not None


# ========================================================== 装饰器的行为
async def test_calls_pass_through_while_under_budget():
    inner = _FakeLLM(per_call=100)
    llm = BudgetedLLM(inner, RunBudget(max_tokens=1_000))
    for _ in range(3):
        await llm.structured(system="s", user="u", schema=Analysis)
    assert inner.calls == 3


async def test_the_budget_is_overshot_by_at_most_one_call():
    """★熔断只能在调用**之前**判，判据是「**已经**烧了多少」。

    所以实际花费一定会**超出预算，最多超一次调用的量**：

        第 1 次  已用 0    < 1000  → 放行，烧到 600
        第 2 次  已用 600  < 1000  → 放行，烧到 1200   ← 超了，但钱已经花了
        第 3 次  已用 1200 ≥ 1000  → 熔断

    熔断器都是这个语义 —— 它保证的是「**不会一直烧下去**」，不是「一分不超」。
    要一分不超，得能预知下一次调用的花费，而那是做不到的。
    """
    inner = _FakeLLM(per_call=600)
    llm = BudgetedLLM(inner, RunBudget(max_tokens=1_000))

    await llm.structured(system="s", user="u", schema=Analysis)
    await llm.structured(system="s", user="u", schema=Analysis)
    with pytest.raises(BudgetExceeded):
        await llm.structured(system="s", user="u", schema=Analysis)

    assert inner.calls == 2, "被熔断的那次不该发出去"
    assert inner.usage.total_tokens == 1_200  # 超出上限 200 = 一次调用的量


async def test_budget_exceeded_is_an_llm_error():
    """★继承关系是刻意的：`execute` 节点已经会捕获 `LLMError`，
    熔断因此天然沿用同一条降级路径，不用额外接线。"""
    assert issubclass(BudgetExceeded, LLMError)

    inner = _FakeLLM()
    inner.usage = Usage(input_tokens=5_000)  # 这个 run 之前已经烧过了
    llm = BudgetedLLM(inner, RunBudget(max_tokens=1_000))

    with pytest.raises(LLMError):
        await llm.structured(system="s", user="u", schema=Analysis)
    assert inner.calls == 0


def test_the_wrapper_forwards_model_and_usage():
    """用量仍然累在真正的客户端上 —— 这一层不持有状态，
    否则 `report_for()` 会和它打架，成本报表就有两个真相来源了。"""
    inner = _FakeLLM()
    inner.usage = Usage(input_tokens=42, calls=1)
    llm = BudgetedLLM(inner, RunBudget())

    assert llm.model == inner.model
    assert llm.usage is inner.usage
    assert llm.name == "fake"


# ============================================================ 装配与节点
def test_build_llm_wraps_the_provider_in_a_circuit_breaker():
    llm = build_llm()
    assert isinstance(llm, BudgetedLLM)
    assert llm.model == "scripted"  # conftest 强制 scripted，穿透到内层


def test_defaults_would_not_have_tripped_on_real_runs():
    """★默认阈值是从实测分布推出来的，不是拍脑袋。

    54 次真实 run：中位 4,258 token、p90 9,009、**最大 39,062**、最贵 $0.0688。
    默认值必须**明显高于实测最大值** —— 否则这个"安全网"会先绊倒正常的 run。
    """
    settings = Settings()
    assert settings.max_run_tokens > 39_062 * 2
    assert settings.max_run_cost_usd > 0.0688 * 10


async def test_evaluate_stops_retrying_instead_of_waiting_for_the_breaker(registry, workspace):
    """★优雅停止：别等下一次调用炸出 `BudgetExceeded`。

    硬熔断在 `BudgetedLLM` 里，但让它抛出来的话报告里只剩一个异常。
    在 `evaluate` 主动停，run 能干净地落到 `failed`，且报告写清楚
    **是预算掐的，不是改不出来** —— 两种失败的处理方式完全不同。
    """
    inner = _FakeLLM()
    inner.usage = Usage(input_tokens=50_000)  # 前面几轮已经把钱烧完了
    nodes = Nodes(
        llm=BudgetedLLM(inner, RunBudget(max_tokens=1_000)),
        registry=registry,
        workspace=workspace,
    )

    state = initial_state("r", "task", "/tmp", max_retries=2)
    state["retry_count"] = 0  # 次数预算还剩着，只有钱没了

    result = await nodes.evaluate(state)
    assert result["verdict"] == "failed", "有重试次数也不能再试了"
    assert "预算" in result["step_log"][0]
    assert any("budget:" in e for e in result["errors"])


async def test_evaluate_tolerates_a_client_without_a_budget(registry, workspace):
    """注入的假客户端 / 自定义 agent 不一定带熔断能力，节点不能因此崩掉。"""
    nodes = Nodes(llm=_FakeLLM(), registry=registry, workspace=workspace)
    state = initial_state("r", "task", "/tmp", max_retries=2)

    result = await nodes.evaluate(state)
    assert result["verdict"] == "retry"


def test_settings_can_turn_the_breaker_off(monkeypatch):
    """两条都置空 = 关掉。安全控制要能关，但**得显式关**。"""
    monkeypatch.setenv("REPOPILOT_MAX_RUN_TOKENS", "")
    monkeypatch.setenv("REPOPILOT_MAX_RUN_COST_USD", "")
    get_settings.cache_clear()
    try:
        from repopilot.llm.budget import budget_from_settings

        assert budget_from_settings().breach(_report(tokens=10**9, cost=10**6)) is None
    finally:
        get_settings.cache_clear()
