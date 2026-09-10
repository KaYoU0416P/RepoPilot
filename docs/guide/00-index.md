# RepoPilot 完全版小白教程 · 总索引

写给一个「Java 后端很熟、Python 一行没写过」的人。
目标不是把你培养成 Python 专家，是让你在面试里**沿着一条调用链，一行不落地讲完这个项目**。

## 怎么用这份文档

有两种读法，选一种，别两种都试。

**读法 A（推荐，赶时间）——「主线优先」**
1. 先读 [05-happy-path.md](05-happy-path.md)（主线逐行）。
2. 读到不认识的语法，**当场**回 [02-syntax.md](02-syntax.md) 查那一小节，查完立刻跳回主线。
3. 主线走通一遍之后，再补 [03-asyncio.md](03-asyncio.md) 和 [04-stack.md](04-stack.md)。
4. 最后扫一遍 [06-faq.md](06-faq.md) 的面试口径。

**读法 B（时间宽裕）——「从下往上」**
按 01 → 02 → 03 → 04 → 05 → 06 顺序读。

## 目录

| 文件 | 内容 | 什么时候读 |
|---|---|---|
| [01-python-kernel.md](01-python-kernel.md) | Python 到底怎么跑起来的、`import` 系统、虚拟环境、`uv`、**本项目完整目录结构逐个解释** | 想知道「文件为什么这么摆」时 |
| [02-syntax.md](02-syntax.md) | **高频语法大全**。每条都配本项目真实出处 + Java 对照 | 当字典查，别通读 |
| [03-asyncio.md](03-asyncio.md) | `async` / `await` / 事件循环 / 并发控制，深入浅出 | 项目 60% 的代码是异步的，必读 |
| [04-stack.md](04-stack.md) | Pydantic、FastAPI、asyncpg + PostgreSQL、LangGraph 四个框架 | 面试问框架时 |
| [05-happy-path.md](05-happy-path.md) | **主线**：一条 `curl` 从进门到 PR，21 站，每行代码拳拳到肉（含 GitHub webhook 这条旁路入口） | 核心，反复读 |
| [06-faq.md](06-faq.md) | 排错手册 + 面试问答口径 + 已知缺口 | 面试前一天 |

## 一句话记住这个项目

> **把「AI 改代码」这件不可靠的事，包进一套可靠的后端工程里。**
>
> 请求进来先落库再返回（任务不丢）→ worker 抢着领（并发安全）→ 领取带租约（进程崩了能回收）
> → Agent 在仓库副本里干活（改不坏真东西）→ 跑测试判定成败（不听 AI 自称）
> → 卡在人工审批（AI 不能自己发布）→ 批准后才开 PR。

架构图、状态机、模块职责表在 [../architecture.md](../architecture.md)。
面试要点的精简版在 [../learning.md](../learning.md)（这份 guide 是它的展开版）。

## 环境速查

```bash
make sync      # 装依赖（永远用这个，不要 uv sync，原因见 01 章 §6）
make db-up     # 起 PostgreSQL（Docker，端口 5433）
make test      # 跑测试，应该 276 passed
make demo      # 不起服务、不用 API key，单跑一次 Agent
make run       # 起 API，浏览器开 http://localhost:8000/docs
make psql      # 进数据库命令行
make db-reset  # 改了 schema.sql 之后删库重建
```

Python 版本：3.12。虚拟环境在 `.venv/`。IDE：VSCode（`.vscode/settings.json` 已配好）。
