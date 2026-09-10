"""跑评测基准集。

    uv run python scripts/bench.py                    # 跑全部 18 个
    uv run python scripts/bench.py --only off-by-one  # 只跑指定的
    uv run python scripts/bench.py --json out.json    # 顺便存一份机读报表

选中的 provider 没有 key 时会降级到 ScriptedLLM，那时**分数没有意义** ——
ScriptedLLM 只认识内置样例仓库，在这 18 个 case 上基本全错。
脚本会明确警告，别拿那个数字当结果。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from repopilot.config import PROJECT_ROOT, get_settings
from repopilot.evaluation.bench import format_report, load_cases
from repopilot.evaluation.harness import BenchHarness
from repopilot.observability import setup_logging, setup_tracing

CASES_ROOT = PROJECT_ROOT / "benchmarks" / "cases"


async def main() -> int:
    parser = argparse.ArgumentParser(description="RepoPilot 评测基准集")
    parser.add_argument("--only", help="逗号分隔的 case id，只跑这些")
    parser.add_argument("--json", type=Path, help="把报表另存为 JSON")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    setup_tracing(enabled=settings.otel_enabled, service_name=settings.otel_service_name)

    if settings.llm_provider == "scripted":
        print(
            "\n⚠  当前是 ScriptedLLM（没有配 provider 的 API key）。\n"
            "   它只认识内置样例仓库，在这些 case 上会几乎全错。\n"
            "   这一轮只能验证 harness 通不通，分数没有意义。\n",
            file=sys.stderr,
        )
    else:
        # 报表里的成本按模型单价算 —— 跑之前就该看见自己在跑哪个模型，
        # 而不是跑完对着一个 `cost_usd: null` 猜是哪里错了。
        known = settings.pricing_for(settings.model) is not None
        priced = "有单价" if known else "★定价表里没有，成本会是 null"
        print(
            f"\n▶  provider={settings.llm_provider}  model={settings.model}（{priced}）\n",
            file=sys.stderr,
        )

    only = args.only.split(",") if args.only else None
    cases = load_cases(CASES_ROOT, only=only)
    if not cases:
        print(f"没找到任何 case（root={CASES_ROOT}, only={only}）", file=sys.stderr)
        return 1

    report = await BenchHarness(settings).run_all(cases)
    print(format_report(report))

    if args.json:
        args.json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"报表已写入 {args.json}")

    # broken_case 说明评测集自己坏了，这比 Agent 表现差严重得多，要用退出码报出来
    return 2 if report.broken_cases else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
