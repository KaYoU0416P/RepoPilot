# RepoPilot 架构

## 是什么

一个受控的代码修复服务。任务进来 → Agent 在隔离副本里改代码 → 跑测试 →
失败按预算重试 → **产物停在人工审批闸门** → 批准后才发布。

两条设计主线，缺一不可：

1. **不信任模型输出** —— LLM 生成的代码绝不在宿主机上无约束执行。
2. **不信任进程活着** —— worker 崩了任务必须能被别人接手，靠租约不靠进程。

## 主调用链

```
POST /webhooks/github         api/routes.py::github_webhook
  ├─▶ verify_signature        对 raw bytes 做 HMAC-SHA256，常数时间比较
  │                           失败 → 401，且**什么都不记账**
  ├─▶ claim_delivery          X-GitHub-Delivery 当幂等键，重投直接 200
  ├─▶ extract_issue_trigger   没打 repopilot 标签 → 忽略（仍返回 2xx）
  └─▶ runs_repo.create_run    source='github_issue'，汇入下面同一条链路

POST /runs                    api/routes.py::create_run
  └─▶ runs_repo.create_run    INSERT ... status='queued'，立刻返回 202
                              （HTTP 不等 Agent，Agent 可能跑几分钟）

Worker.run_forever            worker/worker.py       ← 后台常驻
  ├─▶ Semaphore.acquire       先占坑再去捞任务，没空位就不捞
  ├─▶ runs_repo.claim_next_run    CLAIM_SQL: FOR UPDATE SKIP LOCKED + 租约
  └─▶ Runner.execute          worker/runner.py
        ├─▶ WorkspaceManager.create    copytree + git baseline commit
        ├─▶ heartbeat_loop            后台续租，防止长任务被抢走
        ├─▶ graph.astream             LangGraph
        │     analyze   → list_files, LLM → Analysis
        │     plan      → read_file/search_code（gather 并发）, LLM → Plan
        │     execute   → read_file, LLM → EditSet, write_file
        │     run_tests → sandbox 子进程，墙钟超时
        │     evaluate  → verdict: success | retry | failed
        │       条件边：retry → execute，否则 → finish
        │     finish    → git_diff + 报告
        ├─▶ evaluate_run              evaluation/metrics.py
        └─▶ transition(PENDING_APPROVAL 或 FAILED)   ← 成功不等于结束

POST /runs/{id}/approval      approvals_repo.decide
  ├─▶ transition(PUBLISHING / REJECTED)   守卫 + 乐观锁 + 交还租约
  └─▶ INSERT approvals                    追加写，保留审批历史

Worker._publish_loop           worker/worker.py       ← 和领取循环并排跑
  ├─▶ claim_next_publishing    同一套 SKIP LOCKED + 租约，只是捞 publishing
  └─▶ GitHubPublisher.publish  publishing/github.py
        ├─▶ git clone repo_path → 建确定性分支 → apply(runs.diff) → commit
        ├─▶ git push            重复推同样的提交是 no-op，天然幂等
        ├─▶ find_pull_request   ★分支名当幂等键，已有 PR 就复用不重开
        ├─▶ create_pull_request
        └─▶ comment_on_issue    靠 external_ref 知道回哪个 Issue
      └─▶ transition(PUBLISHED, pr_url=...)  或 PublishError → FAILED

GET /runs/{id}/events         SSE，每个节点完成推一帧
GET /runs/{id}                最终 diff + evaluation
```

## 状态机

```
queued ──▶ running ──▶ pending_approval ──▶ publishing ──▶ published
  │          │  │            │
  │          │  └─▶ failed   └─▶ rejected
  │          └─▶ queued（租约过期，退回队列）
  └─▶ cancelled
```

`domain/status.py` 的 `TRANSITIONS` 是唯一真相来源；终态从表推导，不手写第二份清单。
`test_status.py` 用 BFS 验证「每个活跃状态都能走到某个终态」，防止出现死角。

**关键约束：`running` 不能直接到 `published`。** 这一条就是审批闸门存在的全部意义 ——
Agent 自己说成功不算数，必须有人看过 diff。

## 模块职责

| 模块 | 负责 | 不知道 |
|---|---|---|
| `api/` | HTTP、DTO、SSE | LangGraph 内部、SQL |
| `github/` | webhook 验签、事件解析、REST 客户端 | 数据库、FastAPI |
| `publishing/` | 建分支、push、开 PR、回写评论 | 队列、状态机 |
| `domain/` | 状态机 | 数据库、HTTP |
| `db/` | 表结构、仓储、队列 SQL | Agent、工具 |
| `worker/` | 领取循环、限流、租约、事件总线 | 具体在跑什么图 |
| `agent/` | State、节点、边、提示词 | HTTP、子进程 |
| `tools/` | 工具契约与注册表、超时与并发上限 | 图、LLM |
| `workspace/` | 仓库副本、路径收敛 | 工具、Agent |
| `sandbox/` | 带硬超时的进程执行 | 在跑什么 |
| `llm/` | 供应商适配、结构化输出 | 工具、workspace |
| `evaluation/` | 轨迹指标、评测基准集与判分 | HTTP、LLM、数据库 |

依赖单向：`api → worker → agent → tools → {workspace, sandbox}`；
`db`、`domain`、`llm`、`evaluation` 是叶子。

## 三层隔离

| 风险 | 措施 | 代码位置 |
|---|---|---|
| 模型写到仓库外 | `Workspace.resolve()` 拒绝绝对路径、`..`、符号链接逃逸 | `workspace/manager.py` |
| 生成的代码不终止 | 墙钟超时 + `os.killpg` 杀整个进程组 | `sandbox/local.py` |
| 改坏真实仓库 | 全程操作 `copytree` 副本 | `workspace/manager.py` |

**刻意没有通用 shell 工具**。唯一的执行类工具是 `run_tests`，命令行是写死的。
有了 shell，上面所有限制都变成装饰品。

## 可靠性

| 机制 | 做法 | 面试对照 |
|---|---|---|
| 任务不丢 | 落库后才返回 202，worker 异步领取 | MQ 的持久化 |
| worker 崩了 | 租约到期，任务自动可被重新领取 | MQ 的 ack 超时重投 |
| 长任务不被抢 | 心跳续租，失去所有权时续租失败 | 消费者续期 |
| 无限重试 | `attempts < max_attempts` + reaper 标记失败 | 死信队列 |
| 重复触发 | `webhook_deliveries` 唯一约束 | 幂等键 |
| 重复开 PR | 确定性分支名 + 开 PR 前先查同 head 的 PR | 幂等键（业务层） |
| 发布失败分类 | `PublishError` → 不重试；其他异常 → 租约过期后重试 | 死信 vs 重投 |
| 并发写冲突 | `UPDATE ... WHERE status = 当前状态` | 乐观锁 / @Version |
| 优雅停机 | 停止领新任务 → 等在跑的收尾 → 超时取消，靠租约回收 | 优雅下线 |

## 预算与限流

| 预算 | 位置 | 默认 |
|---|---|---|
| 同时跑几个 Agent | `Worker._slots` Semaphore | 2 |
| 单个 Agent 内工具并发 | `ToolRegistry._semaphore` | 4 |
| Agent 重试 | `evaluate` 节点 vs `max_retries` | 2 |
| 任务被领取次数 | `runs.max_attempts` | 3 |
| 工具超时 | `asyncio.wait_for` | 20s |
| 测试超时 | `sandbox.run_command` | 60s |
| 租约 | `lease_seconds` | 120s |

**注意后两个是乘的关系**：最坏情况同时有 `2 × 4 = 8` 个工具在跑。

## LLM 供应商

`llm/build_llm()` 返回 `AnthropicLLM`（强制 tool use 拿结构化输出）或 `ScriptedLLM`。

> **`ScriptedLLM` 是确定性测试替身，不是 Agent。** 它有一张只认识内置样例仓库的
> 硬编码规则表，存在的意义是让整个图能离线跑测试、不花 token。它「解决」的问题
> 只证明流水线是通的，不证明模型聪明。没有 `ANTHROPIC_API_KEY` 时会自动降级到它。

`publishing/build_publisher()` 同理：没有 `GITHUB_TOKEN` 时降级成 `DryRunPublisher`，
状态照样走到 `published`，但 **`pr_url` 是空的** —— 空的 pr_url 就是「这次没真发」的标记。

**两种缺配置的处理为什么不一样**：webhook 验签缺密钥直接拒绝（fail closed），
发布缺 token 降级继续。因为**验签是安全边界，发布是功能**。安全边界宁可不可用，
功能宁可降级。这条区分要能主动讲。

## 评测

`benchmarks/cases/` 15 个 seeded bug，`make bench` 出报表。详见 `benchmarks/README.md`。

评测链路**刻意绕开数据库、队列和审批**：它要回答的是「Agent 修 bug 行不行」，
掺进基础设施只会让一次失败分不清是谁的问题。

```
BenchHarness.run_case          evaluation/harness.py
  ├─▶ 基线自检     一次性副本上跑隐藏测试，必须失败（否则 broken_case）
  ├─▶ Agent 修     另一份干净副本，全程看不到 verify/
  └─▶ 判分         把 verify/ 拷进去再跑 —— 这才是事实
```

两条铁律：**判分不看 Agent 自述**（不一致就是 `false_success`），
**判分用的测试 Agent 看不见**（否则删测试就是最省事的通关方式）。

## 当前状态

**已完成**：Agent 闭环、6 个工具、Postgres 业务层（队列 + 幂等 + 审批）、
worker 租约与限流、SSE、GitHub webhook 入口、发布链路（PR + 评论）、
15 个 case 的评测基准集，170 个测试。
**`Issue → Run → 审批 → PR` 整条链路已闭环，且能被量化评测。**

**未完成**：clone 陌生仓库（webhook 入队时 `repo_path` 还是内置样例）、
Docker sandbox、MCP server、OpenTelemetry、API 鉴权。详见 `docs/progress.md`。
