"""成本熔断：一个 run 烧超过预算就掐掉。

**这是「成本控制」缺的那一半。** 之前只有记账（`usage.py`），没有刹车 ——
`max_retries` 是**次数**预算不是**金额**预算，一个 run 完全可以在次数以内
烧掉任意多的 token。真实评测里撞到过三次：无解的题上模型反复推理，
一次调用就把 16,384 个输出 token 烧光，产出为零。

## 为什么主控是 token，不是美元

美元预算有一个致命的空档：**定价表里查不到的模型，成本是 `None`** ——
这是 `estimate_cost` 刻意的设计（把未知报成免费是最容易骗自己的地方）。
可 `None` 没法比大小，于是美元熔断在最需要它的时候（用了个没登记的新模型）
恰好失效。

而 **token 永远算得出来**，不依赖任何外部表。所以：

  * `max_run_tokens` 是**主控**，任何时候都生效
  * `max_run_cost_usd` 是**补充**，算得出成本时才参与判断

两条谁先触发都算触发。

## 为什么做成装饰器而不是改两个客户端

`BudgetedLLM` 包住任意一个 `LLMClient`，自己也满足这个协议。于是熔断逻辑
只写一遍，Anthropic / DeepSeek / 以后新增的 provider 全都自动有。

这是 `LLMClient` 收敛成**单方法协议**的第四次兑现（前三次：加计量、加 span、
换供应商）。**Java 对照**：装饰器模式 / Spring 的 AOP 代理 ——
接口越窄，能套在外面的东西越多。

## 熔断只能在调用**之前**判

没法预知一次调用要花多少，所以判据是「**已经**烧了多少」：超了就不再发起
下一次。这意味着**实际花费会略微超出预算**（最后那次调用的钱已经花了）。
熔断器都是这个语义 —— 它保证的是「不会一直烧下去」，不是「一分不超」。

## ★预算是 per-**attempt** 的，不是 per-task

计数器活在客户端实例上，而 `build_llm()` 每次领取任务都新建一个。所以一个
任务被租约回收、重新领取之后，**预算是从零开始的**。

这是对的（每次尝试都该有完整的预算），但意味着最坏花费是**乘**的：

    runs.max_attempts × max_run_tokens

和「`Worker._slots` × `ToolRegistry._semaphore` 也是乘的关系」是同一类账，
配额要按乘积算，不是按单项算。

## 两条停止路径，分工不同

  * **优雅**：`evaluate` 节点在决定重试之前问一句 `breach()`，超了就直接落
    `verdict=failed`，报告里写清楚「是预算掐的，不是改不出来」。
    这是**正常情况下会走的那条**。
  * **硬兜底**：这里抛 `BudgetExceeded`。`analyze` / `plan` 不捕获 `LLMError`，
    所以会一路冒到 `runner.py` 的兜底 except，把 run 标成 FAILED 并把原因
    存进库。丑，但**不会继续烧钱**，而这正是熔断器的唯一职责。
"""

from dataclasses import dataclass

from repopilot.llm.base import LLMError
from repopilot.llm.usage import Usage, UsageReport, report_for


class BudgetExceeded(LLMError):
    """预算烧完了。

    继承 `LLMError` 是刻意的：`execute` 节点已经会捕获 `LLMError` 并把它
    转成一条 error 状态，熔断因此天然沿用同一条降级路径，不用额外接线。
    """


@dataclass(frozen=True)
class RunBudget:
    """一个 run 的花费上限。两条都可以是 `None`（= 不限）。"""

    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def breach(self, report: UsageReport) -> str | None:
        """已经超了吗？超了返回一句人话，没超返回 `None`。

        返回字符串而不是 bool，是因为**调用方需要把原因写进报告和日志** ——
        「预算掐了」和「掐的是哪一条、当时烧到多少」是两件事，后者才可排查。
        """
        used = report.usage.total_tokens
        if self.max_tokens is not None and used >= self.max_tokens:
            return f"token 预算耗尽：已用 {used:,} / 上限 {self.max_tokens:,}"

        # ★成本可能是 None（定价表里没这个模型）。`None` 不参与比较 ——
        # 把「算不出来」当成「没超」是对的：主控是上面那条 token 预算。
        cost = report.cost_usd
        if self.max_cost_usd is not None and cost is not None and cost >= self.max_cost_usd:
            return f"成本预算耗尽：已花 ${cost:.4f} / 上限 ${self.max_cost_usd:.2f}"
        return None


def budget_from_settings() -> RunBudget:
    from repopilot.config import get_settings

    settings = get_settings()
    return RunBudget(
        max_tokens=settings.max_run_tokens,
        max_cost_usd=settings.max_run_cost_usd,
    )


class BudgetedLLM:
    """给任意 `LLMClient` 套一层熔断。自己也满足 `LLMClient` 协议。

    `model` / `usage` 都直接转发给内层 —— 计量仍然累在真正的客户端上，
    这一层不持有任何状态，也就不会和 `report_for()` 打架。
    """

    def __init__(self, inner: object, budget: RunBudget) -> None:
        self._inner = inner
        self._budget = budget

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "budgeted")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    @property
    def usage(self) -> Usage:
        return getattr(self._inner, "usage", None) or Usage()

    def breach(self) -> str | None:
        """现在超预算了吗。给 `evaluate` 节点用 —— 它要在决定重试**之前**问一句。"""
        return self._budget.breach(report_for(self._inner))

    async def structured(self, *, system: str, user: str, schema):
        reason = self.breach()
        if reason is not None:
            raise BudgetExceeded(f"{reason}（本次调用已被熔断，未发出）")
        return await self._inner.structured(system=system, user=user, schema=schema)
