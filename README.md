# RepoPilot

把 Coding Agent 接进真实研发流程的后端服务。

任务入队 → worker 领取 → Agent 在隔离副本里改代码并跑测试 → 按预算重试 →
**产物停在人工审批闸门** → 批准后才发布。

技术栈：FastAPI · LangGraph · Pydantic v2 · asyncio · PostgreSQL 17 · asyncpg

```
POST /runs ─▶ [runs 表 queued] ─▶ Worker 领取(SKIP LOCKED + 租约)
                                      │
                          analyze ─▶ plan ─▶ execute ─▶ run_tests ─▶ evaluate
                                               ▲                        │
                                               └──── retry(有预算) ──────┘
                                      │
                              pending_approval ──▶ 人工审批 ──▶ publishing
```

## 快速开始

```bash
make db-up         # 起 Postgres（端口 5433）
make test          # 118 passed
make demo          # 单跑一次 Agent，不用起服务、不用 API key
make run           # uvicorn :8000，浏览器开 /docs 有 Swagger UI
```

不需要 API key。没有 `ANTHROPIC_API_KEY` 时自动降级到 `ScriptedLLM`
（确定性测试替身，只认识内置样例仓库）。配 `.env` 后走真实模型，见 `.env.example`。

### 跑一次完整业务链路

```bash
RID=$(curl -s -X POST localhost:8000/runs -H 'content-type: application/json' \
  -d '{"task":"Fix divide() so dividing by zero raises ValueError"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["run_id"])')

curl -sN localhost:8000/runs/$RID/events        # SSE，每个节点一帧
curl -s  localhost:8000/runs/$RID | python3 -m json.tool   # → pending_approval

curl -s -X POST localhost:8000/runs/$RID/approval \
  -H 'content-type: application/json' \
  -d '{"decision":"approved","decided_by":"me","reason":"diff 看过了"}'   # → publishing

curl -s -X POST localhost:8000/runs/$RID/approval \
  -H 'content-type: application/json' -d '{"decision":"approved","decided_by":"me"}'
# → 409，不能批准两次
```

数据库可视化：DBeaver 连 `localhost:5433`，库/用户/密码都是 `repopilot`。

## 设计要点

### 不信任模型输出

| 风险 | 措施 |
|---|---|
| 模型写到仓库外 | `Workspace.resolve()` 拒绝绝对路径、`..`、符号链接逃逸 |
| 生成的代码不终止 | 墙钟超时 + `os.killpg` 杀整个进程组 |
| 改坏真实仓库 | 全程操作 `copytree` 出来的副本 |
| Agent 自称成功 | 用测试结果判定，且状态机不允许 `running` 直达 `published` |

**刻意没有通用 shell 工具**。唯一的执行类工具是 `run_tests`，命令行写死。

### 不信任进程活着

| 机制 | 做法 | 对照 |
|---|---|---|
| 任务不丢 | 落库后才返回 202，worker 异步领取 | MQ 持久化 |
| worker 崩了 | 租约到期任务自动可被重领 | MQ ack 超时重投 |
| 长任务不被抢 | 心跳续租；失去所有权时续租失败 | 消费者续期 |
| 无限重试 | `attempts < max_attempts` + reaper | 死信队列 |
| 重复触发 | `webhook_deliveries` 唯一约束 + `ON CONFLICT DO NOTHING` | 幂等键 |
| 并发写冲突 | `UPDATE ... WHERE status = 当前状态` | 乐观锁 / `@Version` |
| 优雅停机 | 停止领新任务 → 等收尾 → 超时取消靠租约回收 | 优雅下线 |

队列直接用 `runs` 表：`FOR UPDATE SKIP LOCKED` + 租约。队列和业务表是同一张表，
入队和业务写入天然同事务，不存在双写不一致。取舍见 `docs/learning.md`。

### 两层限流

`Worker._slots`（同时几个 Agent，默认 2）× `ToolRegistry._semaphore`
（单个 Agent 内工具并发，默认 4）—— 是**乘**的关系，最坏 8 个工具同时在跑。

## 评测

不只看最终输出，也看轨迹：

```json
{
  "task_success": true, "tests_passed": true, "retry_count": 0,
  "tool_calls_total": 7, "tool_calls_failed": 0,
  "tool_selection": {"list_files":1,"read_file":2,"search_code":1,"write_file":1,"run_tests":1},
  "diff_valid": true, "failure_reason": "none"
}
```

## 目录

```
src/repopilot/
  api/            FastAPI 路由、DTO、SSE
  domain/         状态机（唯一真相来源）
  db/             schema、仓储、队列 SQL、幂等、审批
  worker/         领取循环、限流、租约、事件总线
  agent/          AgentState、节点、图、提示词
  tools/          工具契约与注册表
  workspace/      仓库副本、路径收敛
  sandbox/        带硬超时的进程执行
  llm/            供应商适配、结构化输出
  evaluation/     轨迹指标
db/schema.sql     3 张表：runs / webhook_deliveries / approvals
fixtures/sample_repo/   演示与测试用的目标仓库
github/         webhook 验签（HMAC-SHA256）、事件解析、REST 客户端
publishing/     Publisher 协议 + 开 PR / 回写评论 + 无 token 时空转
docs/             架构、进度、面试笔记、故障复盘
docs/guide/       小白完全版教程（语法、内核、框架、主线逐行）
```

> 零基础入门看 [docs/guide/](docs/guide/00-index.md) —— 从 Python 语法一路讲到主线每一行代码。

## 状态

**已完成**：Agent 闭环、6 个工具、Postgres 业务层（队列 + 幂等 + 审批闸门）、
租约与限流、SSE、评测指标、GitHub 全链路（webhook 验签 → 入队 → 开 PR → 回写评论）、
118 个测试。

**未完成**（诚实列出）：评测基准集、Docker sandbox、MCP server、
OpenTelemetry、API 鉴权。事件总线是进程内的，拆多进程需换 Redis pub/sub 或
PG `LISTEN/NOTIFY`。详见 [docs/progress.md](docs/progress.md)。
