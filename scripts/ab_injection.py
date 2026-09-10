"""注入防护的 A/B 对照实验：关掉防御再跑一遍同样的攻击样本。

    uv run python scripts/ab_injection.py --rounds 3

**这个脚本存在的理由，是一个诚实性问题。**

三轮评测里注入样本 9/9 全都抵抗住了。但那个数字**证明不了防御有效** ——
它和「模型本来就不上当」完全无法区分。要把「没被攻破」升级成
「因为我的防御所以没被攻破」，唯一的办法是做对照：**关掉防御再跑一遍**。

## 为什么用 monkeypatch，而不是加一个配置开关

最省事的做法是给 `Settings` 加一个 `injection_defense_enabled: bool`。
**刻意不这么做**：一个能通过环境变量关掉安全控制的开关，本身就是一个漏洞 ——
它会出现在生产配置里，会被误设，会在某次排障时被"临时"关掉然后忘记打开。
`MAX_TASK_CHARS` 不放进 config.py 是同一个判断。

实验代码就该待在实验脚本里，靠 monkeypatch 在**进程内**临时改写，
跑完即消失，不在生产表面留任何痕迹。

## 关掉的是「三件套」，不是只拆围栏

防御由三部分组成，只关一部分就不是干净的对照：

  1. `fence_task()` 的**中和**（干掉正文自带的闭合标签）
  2. `fence_task()` 的**截断**（4000 字符上限）
  3. SYSTEM 里的 `# Untrusted input` 段落（声明围栏内是数据不是指令）

OFF 组把三样一起去掉，等于回到 Stage D 第一步之前那版代码。

## 配对交错，不是先跑完 A 再跑 B

每一轮里对每个 case 连着跑 ON 和 OFF。两个原因：模型服务端的负载会漂、
DeepSeek 还有峰谷定价，先跑完一组再跑另一组会让「时段」和「分组」共线；
配对之后两组面对的是几乎同一时刻的服务端。这就是 A/B 测试里的**区组化**。
"""

import argparse
import asyncio
import contextlib
import sys
from collections import Counter
from pathlib import Path

from repopilot.agent import prompts
from repopilot.config import PROJECT_ROOT, get_settings
from repopilot.evaluation.bench import CaseResult, load_cases
from repopilot.evaluation.harness import BenchHarness
from repopilot.observability import setup_logging

CASES_ROOT = PROJECT_ROOT / "benchmarks" / "cases"

#: SYSTEM 里防护段落的起点。OFF 组从这里整段砍掉。
_DEFENCE_HEADING = "\n# Untrusted input"


@contextlib.contextmanager
def defence_disabled():
    """在进程内临时把三件套一起摘掉，退出时原样装回。

    `nodes.py` 是 `prompts.fence_task(...)` / `prompts.SYSTEM` 这样**按模块属性**
    取值的，不是 `from ... import fence_task`。所以改模块属性就能生效 ——
    这也是当初那样写的好处之一，虽然当时不是为了这个。
    """
    original_fence, original_system = prompts.fence_task, prompts.SYSTEM
    head = prompts.SYSTEM.split(_DEFENCE_HEADING, 1)[0]
    assert head != prompts.SYSTEM, "SYSTEM 里找不到防护段落，A/B 会变成 A/A"
    try:
        prompts.fence_task = lambda task: task  # 不中和、不截断、不包围栏
        prompts.SYSTEM = head
        yield
    finally:
        prompts.fence_task, prompts.SYSTEM = original_fence, original_system


async def main() -> int:
    parser = argparse.ArgumentParser(description="注入防护 A/B 对照")
    parser.add_argument("--rounds", type=int, default=3, help="每组各跑几轮")
    parser.add_argument("--json", type=Path, help="把原始结果另存为 JSON")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    if settings.llm_provider == "scripted":
        print("⚠  ScriptedLLM 不会被注入，这个实验必须用真实模型。", file=sys.stderr)
        return 1

    cases = [c for c in load_cases(CASES_ROOT) if c.injection is not None]
    if not cases:
        print("没有注入 case", file=sys.stderr)
        return 1

    harness = BenchHarness(settings)
    runs: dict[str, list[CaseResult]] = {"on": [], "off": []}

    total = args.rounds * len(cases) * 2
    done = 0
    for round_index in range(args.rounds):
        for case in cases:
            # ★配对交错：同一个 case 的两组挨着跑，尽量面对同一时刻的服务端。
            for arm in ("on", "off"):
                done += 1
                print(
                    f"[{done}/{total}] 第 {round_index + 1} 轮  {case.id}  防御={arm.upper()}",
                    file=sys.stderr,
                )
                if arm == "on":
                    result = await harness.run_case(case)
                else:
                    with defence_disabled():
                        result = await harness.run_case(case)
                runs[arm].append(result)

    print(_format(runs, cases, args.rounds))

    if args.json:
        payload = {
            arm: [r.model_dump(mode="json") for r in results] for arm, results in runs.items()
        }
        import json

        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"原始结果已写入 {args.json}")
    return 0


def _format(runs: dict[str, list[CaseResult]], cases, rounds: int) -> str:
    lines = [
        "",
        "=" * 78,
        f"注入防护 A/B 对照   每组 {len(cases)} 个样本 × {rounds} 轮",
        "=" * 78,
        "",
        f"{'case':<30}{'防御 ON':<24}{'防御 OFF':<24}",
        "-" * 78,
    ]
    for case in cases:
        row = [f"{case.id:<30}"]
        for arm in ("on", "off"):
            picked = [r for r in runs[arm] if r.case_id == case.id]
            landed = sum(r.injection_landed for r in picked)
            counts = Counter(r.outcome for r in picked)
            summary = " ".join(f"{k}×{v}" for k, v in counts.most_common())
            # 注意别写成 `{x::<24}` —— 多一个冒号会把 ':' 当成填充字符。
            cell = summary + (f" ⚠{landed}落地" if landed else "")
            row.append(f"{cell:<26}")
        lines.append("".join(row))

    lines += ["", "汇总", "-" * 78]
    for arm, label in (("on", "防御 ON "), ("off", "防御 OFF")):
        results = runs[arm]
        landed = sum(r.injection_landed for r in results)
        resisted = sum(r.outcome == "resisted" for r in results)
        hijacked = sum(r.outcome == "hijacked" for r in results)
        cost = sum(r.usage.cost_usd or 0.0 for r in results)
        lines.append(
            f"  {label}   载荷落地 {landed}/{len(results)}   "
            f"resisted {resisted}   hijacked {hijacked}   ${cost:.4f}"
        )

    on_landed = sum(r.injection_landed for r in runs["on"])
    off_landed = sum(r.injection_landed for r in runs["off"])
    lines += ["", "结论", "-" * 78]
    if off_landed > on_landed:
        lines.append(
            f"  ✓ 关掉防御后载荷落地次数从 {on_landed} 升到 {off_landed} —— "
            f"**这是防御起作用的直接证据**。"
        )
    elif off_landed == on_landed == 0:
        lines += [
            "  ⚠ 两组都是 0 次落地 —— **这一轮证明不了防御有效**。",
            "    可能是模型本来就不上当，也可能是这三个载荷对它太弱。",
            "    诚实的说法仍然只能是「没被攻破」，不能说「因为防御所以没被攻破」。",
            "    要往下走：换更强的载荷，或换一个更容易被骗的模型再测。",
        ]
    else:
        lines.append(
            f"  ？ ON 落地 {on_landed} 次、OFF 落地 {off_landed} 次 —— "
            f"样本太少或防御无效，别急着下结论。"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
