# 实测存档

简历上每个数字的出处。**每份都带重跑命令** —— 说得出怎么测的，
比数字本身重要。

| 存档 | 结论 | 重跑 | 花费 |
|---|---|---|---|
| [bench-18x3.md](bench-18x3.md) | 可靠成功率 **89%**，平均修对一个 **$0.0080** | `make bench ARGS="--repeat 3"` | ~$0.40 / 40 分钟 |
| [injection-ab.md](injection-ab.md) | 关掉防御后载荷落地 **0/9 → 4/9** | `uv run python scripts/ab_injection.py` | ~$0.17 |
| [sandbox-isolation.md](sandbox-isolation.md) | 越狱项 **4 → 0** | `make sandbox-check` / `ARGS=--local` | 免费 |
| [end-to-end-flow.md](end-to-end-flow.md) | Issue → 审批 → PR 全链路，热启动 4 秒 | `make demo-flow` | 免费 |

## 两条诚实说明

**评测有随机性，重跑会得到不同的数字。** 这正是要报「可靠成功率」（每一轮都对
才算）而不是单轮成功率的原因。存档的意义是「这个数是怎么来的」，
不是「随时能复现出同一个数」。

**后两份是免费且确定性的**，所以面试现场可以直接跑。
`make sandbox-check` 对照那组尤其有说服力 —— 十几秒，当场看到本地子进程
能读 `.env` 里的 API key 并联网发出去。
