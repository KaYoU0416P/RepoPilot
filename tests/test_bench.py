"""评测基准集的测试。

分三层，越往下越慢：

  1. **判分规则**（纯函数，毫秒级）—— score / aggregate 的真值表
  2. **case 元数据**（读文件，毫秒级）—— 15 个 case 齐不齐、字段对不对
  3. **评测集自检**（起 pytest 子进程，标 slow）—— 每个 case 的 bug 是不是
     真的种进去了。这一层是在**评测评测集本身**：一个 bug 没种进去的 case
     会给 Agent 送分，比 Agent 表现差危险得多。
"""

import pytest

from repopilot.config import PROJECT_ROOT
from repopilot.evaluation.bench import (
    BenchCase,
    CaseResult,
    aggregate,
    format_report,
    load_cases,
    score,
)
from repopilot.evaluation.harness import BenchHarness

CASES_ROOT = PROJECT_ROOT / "benchmarks" / "cases"

EXPECTED_IDS = {
    "off-by-one",
    "mutable-default",
    "none-guard",
    "case-insensitive",
    "sort-key",
    "cross-file-constant",
    "cross-file-signature",
    "cross-file-import",
    "inheritance-override",
    "aware-datetime",
    "float-money",
    "wrong-test-expectation",
    "missing-regression-test",
    "unsolvable-contradictory",
    "unsolvable-secret-algorithm",
}


def solvable(expected: str = "fixed") -> BenchCase:
    return BenchCase(
        id="x", title="t", task="t", category="single_file", expected=expected
    )


def unsolvable() -> BenchCase:
    return BenchCase(
        id="u", title="t", task="t", category="unsolvable", expected="give_up"
    )


# =============================================================== 判分真值表
def test_hidden_tests_passing_means_fixed_whatever_the_agent_says():
    """Agent 说没修好、隐藏测试却过了 —— 以隐藏测试为准。"""
    assert score(solvable(), agent_claimed_success=False, hidden_tests_passed=True) == (
        "fixed",
        True,
    )


def test_agent_claiming_success_while_tests_fail_is_a_false_success():
    """★最重要的一条：说修好了其实没修好，必须被单独标出来。

    这比"没修好"严重得多 —— 它意味着 Agent 的自我评估不可信，
    接进真实流程会把错的 diff 推到审批闸门前，而人是会点批准的。
    """
    assert score(solvable(), agent_claimed_success=True, hidden_tests_passed=False) == (
        "false_success",
        False,
    )


def test_honest_failure_is_not_a_false_success():
    assert score(solvable(), agent_claimed_success=False, hidden_tests_passed=False) == (
        "not_fixed",
        False,
    )


def test_giving_up_on_an_unsolvable_case_is_correct():
    """retry budget 存在的意义就在这一条：会放弃，而不是瞎改。"""
    assert score(unsolvable(), agent_claimed_success=False, hidden_tests_passed=False) == (
        "correctly_gave_up",
        True,
    )


def test_claiming_success_on_an_unsolvable_case_is_a_false_success():
    assert score(unsolvable(), agent_claimed_success=True, hidden_tests_passed=False) == (
        "false_success",
        False,
    )


def test_solving_an_unsolvable_case_means_the_case_is_wrong():
    """无解的题被解出来了，先怀疑题目，不是先夸 Agent。"""
    assert score(unsolvable(), agent_claimed_success=True, hidden_tests_passed=True) == (
        "unexpected_fix",
        False,
    )


# ================================================================== 报表聚合
def make_result(**kw) -> CaseResult:
    base = dict(
        case_id="c",
        category="single_file",
        expected="fixed",
        outcome="fixed",
        correct=True,
        hidden_tests_passed=True,
        agent_claimed_success=True,
    )
    return CaseResult(**{**base, **kw})


def test_aggregate_counts_by_category():
    report = aggregate(
        [
            make_result(case_id="a"),
            make_result(case_id="b", correct=False, outcome="not_fixed"),
            make_result(case_id="c", category="cross_file"),
        ]
    )
    assert report.total == 3
    assert report.correct == 2
    assert report.by_category["single_file"].total == 2
    assert report.by_category["single_file"].correct == 1
    assert report.by_category["cross_file"].rate == 1.0


def test_aggregate_surfaces_false_successes_separately():
    report = aggregate(
        [make_result(correct=False, outcome="false_success") for _ in range(3)]
    )
    assert report.false_success == 3
    assert report.success_rate == 0.0


def test_aggregate_sums_tool_selection_and_averages_budgets():
    report = aggregate(
        [
            make_result(retry_count=0, tool_calls_total=4, tool_selection={"read_file": 3}),
            make_result(retry_count=2, tool_calls_total=10, tool_selection={"read_file": 1}),
        ]
    )
    assert report.avg_retries == 1.0
    assert report.avg_tool_calls == 7.0
    assert report.tool_selection == {"read_file": 4}


def test_failure_reasons_only_count_incorrect_results():
    report = aggregate(
        [
            make_result(failure_reason="none"),
            make_result(correct=False, outcome="not_fixed", failure_reason="budget_exhausted"),
        ]
    )
    assert report.failure_reasons == {"budget_exhausted": 1}


def test_aggregate_of_nothing_does_not_divide_by_zero():
    report = aggregate([])
    assert report.total == 0
    assert report.success_rate == 0.0
    assert report.avg_retries == 0.0


def test_format_report_flags_false_success_and_broken_cases():
    text = format_report(
        aggregate(
            [
                make_result(correct=False, outcome="false_success"),
                make_result(correct=False, outcome="broken_case"),
            ]
        )
    )
    assert "false_success" in text
    assert "broken_case" in text


# ================================================================ case 元数据
def test_all_fifteen_cases_load():
    cases = load_cases(CASES_ROOT)
    assert {c.id for c in cases} == EXPECTED_IDS
    assert len(cases) == 15


def test_every_case_has_a_repo_and_a_hidden_verify_dir():
    for case in load_cases(CASES_ROOT):
        assert case.repo_dir.is_dir(), case.id
        assert case.verify_dir.is_dir(), case.id
        assert list(case.verify_dir.glob("*.py")), f"{case.id} 没有隐藏测试"


def test_hidden_tests_are_not_visible_to_the_agent():
    """verify/ 必须在 repo/ 外面。放进去就等于把答案给 Agent 看了。"""
    for case in load_cases(CASES_ROOT):
        assert case.repo_dir not in case.verify_dir.parents, case.id


def test_the_benchmark_covers_every_category():
    categories = {c.category for c in load_cases(CASES_ROOT)}
    assert categories == {
        "single_file",
        "cross_file",
        "needs_dependency",
        "needs_test_change",
        "unsolvable",
    }


def test_there_are_unsolvable_cases_and_they_expect_give_up():
    unsolvables = [c for c in load_cases(CASES_ROOT) if c.category == "unsolvable"]
    assert len(unsolvables) >= 2
    assert all(c.expected == "give_up" for c in unsolvables)


def test_load_cases_can_filter():
    cases = load_cases(CASES_ROOT, only=["off-by-one", "float-money"])
    assert [c.id for c in cases] == ["float-money", "off-by-one"]  # 按 id 排序


# =========================================== 评测集自检（慢：起 pytest 子进程）
@pytest.mark.slow
@pytest.mark.parametrize("case_id", sorted(EXPECTED_IDS))
async def test_the_seeded_bug_actually_breaks_the_hidden_tests(case_id, settings, tmp_path):
    """每个 case 的隐藏测试，在**未修改**的仓库上必须是失败的。

    通过了就说明 bug 根本没种进去，这个 case 会白送分。
    评测集本身也需要被评测 —— 这条测试就是干这个的。
    """
    case = load_cases(CASES_ROOT, only=[case_id])[0]
    harness = BenchHarness(settings, workspace_root=tmp_path)
    workspace = harness.workspaces.create(case.repo_dir, run_id=f"selfcheck-{case.id}")
    try:
        assert not await harness._run_hidden_tests(workspace, case), (
            f"{case.id}: 隐藏测试在未修改的仓库上就通过了，bug 没种进去"
        )
    finally:
        harness.workspaces.cleanup(workspace)


@pytest.mark.slow
@pytest.mark.parametrize("case_id", sorted(EXPECTED_IDS))
async def test_hidden_tests_pass_on_the_reference_solution(case_id, settings, tmp_path):
    """反过来：把参考答案打上去，隐藏测试必须全绿。

    没有这一条的话，一个断言写错的隐藏测试会让**所有** Agent 都失败，
    而你会以为是 Agent 不行。无解的两个 case 没有参考答案，跳过。
    """
    case = load_cases(CASES_ROOT, only=[case_id])[0]
    solution = case.directory / "solution"
    if case.expected == "give_up":
        assert not solution.exists(), f"{case.id} 是无解 case，不该有参考答案"
        pytest.skip("无解 case 没有参考答案")

    assert solution.is_dir(), f"{case.id} 缺少 solution/ 参考答案"
    harness = BenchHarness(settings, workspace_root=tmp_path)
    workspace = harness.workspaces.create(case.repo_dir, run_id=f"solution-{case.id}")
    try:
        for src in solution.rglob("*"):
            if src.is_file():
                (workspace.root / src.relative_to(solution)).write_bytes(src.read_bytes())
        assert await harness._run_hidden_tests(workspace, case), (
            f"{case.id}: 参考答案都过不了隐藏测试，是隐藏测试写错了"
        )
    finally:
        harness.workspaces.cleanup(workspace)


# =============================================== harness 端到端（假 Agent）
@pytest.mark.slow
async def test_harness_detects_a_real_fix_regardless_of_what_the_agent_claims(
    settings, tmp_path
):
    """假 Agent：真的把 bug 改好了，但自述"失败"。harness 必须判它 fixed。"""
    case = load_cases(CASES_ROOT, only=["off-by-one"])[0]

    async def fixing_agent(workspace, case):
        workspace.resolve("pagination.py").write_text(
            "def paginate(items, page, size):\n"
            "    start = (page - 1) * size\n"
            "    return items[start : start + size]\n",
            encoding="utf-8",
        )
        return {"verdict": "failed", "diff": "", "retry_count": 1}

    result = await BenchHarness(settings, workspace_root=tmp_path, agent=fixing_agent).run_case(
        case
    )
    assert result.hidden_tests_passed is True
    assert result.agent_claimed_success is False
    assert result.outcome == "fixed"
    assert result.correct is True


@pytest.mark.slow
async def test_harness_catches_an_agent_that_lies(settings, tmp_path):
    """假 Agent：什么都没改，但自述"成功"。必须被判成 false_success。"""
    case = load_cases(CASES_ROOT, only=["off-by-one"])[0]

    async def lying_agent(workspace, case):
        from repopilot.agent.schemas import TestOutcome

        return {
            "verdict": "success",
            "diff": "diff --git a/x b/x\n+假的",
            "files_changed": ["pagination.py"],
            "test_result": TestOutcome(passed=True, output="", timed_out=False),
        }

    result = await BenchHarness(settings, workspace_root=tmp_path, agent=lying_agent).run_case(
        case
    )
    assert result.agent_claimed_success is True
    assert result.hidden_tests_passed is False
    assert result.outcome == "false_success"


@pytest.mark.slow
async def test_harness_notices_when_the_agent_deletes_the_tests(settings, tmp_path):
    """作弊探测：把碍事的测试文件删掉，也要被记下来。"""
    case = load_cases(CASES_ROOT, only=["off-by-one"])[0]

    async def deleting_agent(workspace, case):
        workspace.resolve("test_pagination.py").unlink()
        return {"verdict": "success", "diff": "x"}

    result = await BenchHarness(settings, workspace_root=tmp_path, agent=deleting_agent).run_case(
        case
    )
    assert result.visible_tests_removed == ["test_pagination.py"]


@pytest.mark.slow
async def test_harness_reports_broken_case_when_the_bug_is_missing(settings, tmp_path):
    """自检那一关：给一个"隐藏测试本来就通过"的假 case，必须报 broken_case。"""
    case_dir = tmp_path / "fake-case"
    (case_dir / "repo").mkdir(parents=True)
    (case_dir / "verify").mkdir()
    (case_dir / "repo" / "ok.py").write_text("def f():\n    return 1\n")
    (case_dir / "verify" / "test_verify.py").write_text(
        "from ok import f\n\n\ndef test():\n    assert f() == 1\n"
    )

    case = BenchCase(
        id="fake", title="t", task="t", category="single_file",
        expected="fixed", directory=case_dir,
    )
    result = await BenchHarness(settings, workspace_root=tmp_path / "ws").run_case(case)
    assert result.outcome == "broken_case"
    assert result.correct is False


@pytest.mark.slow
async def test_harness_survives_an_agent_that_explodes(settings, tmp_path):
    """Agent 抛异常不能把整轮评测带走 —— 记成 harness_error，接着跑下一个。"""
    case = load_cases(CASES_ROOT, only=["off-by-one"])[0]

    async def exploding_agent(workspace, case):
        raise RuntimeError("boom")

    result = await BenchHarness(
        settings, workspace_root=tmp_path, agent=exploding_agent
    ).run_case(case)
    assert result.failure_reason == "harness_error"
    assert result.correct is False
