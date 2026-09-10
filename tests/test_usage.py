"""Token 计量与成本估算。

全是纯函数，跑得飞快，**一个 token 都不花**。这正是把定价逻辑从
`anthropic_client.py` 里拆出来的理由：想验证「$3/百万输入算得对不对」，
不该需要一个 API key。
"""

import pytest

from repopilot.config import ModelPricing, get_settings
from repopilot.evaluation.bench import CaseResult, aggregate, format_report
from repopilot.llm import ScriptedLLM, build_llm
from repopilot.llm.usage import Usage, UsageReport, estimate_cost

SONNET = ModelPricing(input_per_mtok=3.00, output_per_mtok=15.00)


# ==================================================================== 累加
def test_usage_adds_field_by_field():
    a = Usage(input_tokens=100, output_tokens=10, calls=1)
    b = Usage(input_tokens=50, output_tokens=5, calls=1)
    assert (a + b) == Usage(input_tokens=150, output_tokens=15, calls=2)


def test_adding_does_not_mutate_either_side():
    """Usage 是值对象。`a + b` 之后 a 必须还是原来的 a。

    这条看着像废话，但计量器是被多处读写的共享对象 —— 就地修改的实现
    会让「谁改了我的账」变成一类极难查的 bug。
    """
    a = Usage(input_tokens=100, calls=1)
    _ = a + Usage(input_tokens=50, calls=1)
    assert a.input_tokens == 100


# ============================================== ★三份输入 token 是互斥的三份
def test_total_input_is_the_sum_of_all_three_buckets():
    """★这一条是整个计量最容易搞错的地方。

    `input_tokens` **只是没命中缓存的那部分**。命中缓存的 token 记在
    `cache_read_input_tokens`，写缓存的记在 `cache_creation_input_tokens`。
    三个是互斥的三份，不是同一份的三种视角。

    只看 `input_tokens` 会以为自己特别省 —— 其实只是没把另外两桶加进来。
    """
    usage = Usage(
        input_tokens=1_000,
        cache_creation_input_tokens=4_000,
        cache_read_input_tokens=20_000,
        output_tokens=500,
    )
    assert usage.total_input_tokens == 25_000
    assert usage.total_tokens == 25_500


def test_cache_hit_rate_is_zero_when_nothing_was_cached():
    assert Usage(input_tokens=1_000).cache_hit_rate == 0.0


def test_cache_hit_rate_of_an_empty_usage_does_not_divide_by_zero():
    assert Usage().cache_hit_rate == 0.0


# ================================================================ 成本估算
def test_cost_uses_the_published_per_million_rate():
    """Sonnet 4.6 = $3 / 百万输入、$15 / 百万输出。"""
    cost = estimate_cost(
        Usage(input_tokens=1_000_000, output_tokens=1_000_000), "claude-sonnet-4-6", SONNET
    )
    assert cost.input_usd == pytest.approx(3.00)
    assert cost.output_usd == pytest.approx(15.00)
    assert cost.total_usd == pytest.approx(18.00)


def test_cached_tokens_are_priced_differently_from_fresh_ones():
    """读缓存约 0.1 倍、写缓存约 1.25 倍。缓存省钱就省在读那一侧。"""
    cost = estimate_cost(
        Usage(cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000),
        "claude-sonnet-4-6",
        SONNET,
    )
    assert cost.cache_read_usd == pytest.approx(0.30)  # 3.00 * 0.1
    assert cost.cache_write_usd == pytest.approx(3.75)  # 3.00 * 1.25
    assert cost.total_usd == pytest.approx(4.05)


def test_an_unknown_model_costs_none_not_zero():
    """★把「不知道」报成「免费」是成本报表最容易骗到自己的地方。

    定价表里查不到的模型，成本必须是 `None`，让报表打印「未知」。
    返回 0.0 的话，一个模型 ID 拼错就能让整份账单看起来是免费的。
    """
    cost = estimate_cost(Usage(input_tokens=999_999), "claude-made-up-9", None)
    assert cost.total_usd is None


def test_the_default_pricing_table_covers_the_configured_model():
    """默认跑的那个模型必须在定价表里，否则成本永远是「未知」。"""
    settings = get_settings()
    assert settings.pricing_for(settings.model) is not None, (
        f"config.model={settings.model} 不在 model_prices 里，成本算不出来"
    )


# ======================================== per-run 计量成立的前提：客户端不复用
def test_build_llm_returns_a_fresh_client_every_time():
    """★`usage` 之所以能当「这个 run 的总量」，全靠这一条。

    `build_llm()` 每次新建实例，`runner.py` / `harness.py` 每个 run 各调一次，
    于是「实例累计」== 「run 累计」，节点不用自己做差值。
    哪天有人给 `build_llm` 加个 `lru_cache`，用量就会跨 run 累加，
    这条测试是唯一会拦住他的东西。
    """
    assert build_llm() is not build_llm()


def test_the_scripted_double_counts_calls_but_spends_nothing():
    """测试替身要能撑起计量链路，但成本恒为 0 —— 「测试不花钱」的断言口子。"""
    llm = ScriptedLLM()
    assert llm.usage.calls == 0
    assert llm.usage.total_tokens == 0


# ====================================== 从 Anthropic 响应里提取用量（不联网）
class _FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeResponse:
    def __init__(self, usage):
        self.usage = usage


def test_all_four_token_fields_are_read_off_the_response():
    from repopilot.llm.anthropic_client import _usage_of

    usage = _usage_of(
        _FakeResponse(
            _FakeUsage(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=300,
                cache_read_input_tokens=4_000,
            )
        )
    )
    assert usage.total_input_tokens == 4_400
    assert usage.calls == 1


def test_missing_cache_fields_count_as_zero_instead_of_exploding():
    """两个 cache 字段只有请求带了 `cache_control` 时才有值，也可能是 None。

    计量不该比业务还脆 —— 少一个字段就把整个 run 带崩是最蠢的失败方式。
    """
    from repopilot.llm.anthropic_client import _usage_of

    usage = _usage_of(
        _FakeResponse(_FakeUsage(input_tokens=10, output_tokens=2, cache_read_input_tokens=None))
    )
    assert usage.cache_read_input_tokens == 0
    assert usage.cache_creation_input_tokens == 0
    assert usage.total_tokens == 12


# ================================================== 报表：成本怎么被聚合出来
def _case(case_id: str, *, correct: bool, tokens: int, cost: float | None) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        category="single_file",
        expected="fixed",
        outcome="fixed" if correct else "not_fixed",
        correct=correct,
        hidden_tests_passed=correct,
        agent_claimed_success=correct,
        usage=UsageReport(
            model="claude-sonnet-4-6",
            usage=Usage(input_tokens=tokens, calls=3),
            cost_usd=cost,
        ),
    )


def test_cost_per_correct_case_charges_the_failures_to_the_successes():
    """★简历上那个数字。

    分母是「判对的 case 数」，不是总数：失败也烧钱，那部分成本必须摊到成功上。
    否则一个「全部失败但很便宜」的 Agent 会显得性价比最高。
    """
    report = aggregate(
        [
            _case("a", correct=True, tokens=1_000, cost=0.01),
            _case("b", correct=False, tokens=2_000, cost=0.03),
        ]
    )
    assert report.total_cost_usd == pytest.approx(0.04)
    assert report.cost_per_correct_usd == pytest.approx(0.04)  # 0.04 / 1 个对的


def test_one_unpriced_case_makes_the_whole_total_unknown():
    """有一个 case 算不出成本，总额就不可信 —— 报 None，别报一个偏小的数。"""
    report = aggregate(
        [
            _case("a", correct=True, tokens=1_000, cost=0.01),
            _case("b", correct=True, tokens=1_000, cost=None),
        ]
    )
    assert report.total_cost_usd is None
    assert report.cost_per_correct_usd is None


def test_failure_token_overhead_compares_failed_cases_against_successful_ones():
    """失败 case 比成功 case 多烧多少 —— 用数据说，别用直觉说。"""
    report = aggregate(
        [
            _case("a", correct=True, tokens=1_000, cost=0.01),
            _case("b", correct=False, tokens=1_500, cost=0.01),
        ]
    )
    assert report.failure_token_overhead == pytest.approx(0.5)  # 多烧 50%


def test_failure_overhead_is_none_when_there_is_nothing_to_compare():
    """全对（或全错）时没有对照组。除数为 0 的时候不要编一个数出来。"""
    report = aggregate([_case("a", correct=True, tokens=1_000, cost=0.01)])
    assert report.failure_token_overhead is None


def test_scripted_runs_report_zero_tokens_without_dividing_by_zero():
    """ScriptedLLM 跑出来的报表全是 0 token。聚合不能因此炸掉。"""
    report = aggregate(
        [
            _case("a", correct=True, tokens=0, cost=None),
            _case("b", correct=False, tokens=0, cost=None),
        ]
    )
    assert report.avg_tokens == 0.0
    assert report.failure_token_overhead is None
    assert "未知" in format_report(report)


def test_report_prints_the_cost_per_correct_case():
    text = format_report(aggregate([_case("a", correct=True, tokens=100, cost=0.02)]))
    assert "平均修对一个" in text
    assert "$0.0200" in text
