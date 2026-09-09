# Progress

## DONE

### Stage 0 — Agent MVP
- uv + pyproject，src 布局，ruff，pytest（asyncio_mode=auto）。
- `workspace/`：每次运行 `copytree` 一份仓库副本 + git baseline commit，`resolve()` 做路径收敛。
- `sandbox/local.py`：不过 shell 的子进程、墙钟超时、按进程组 kill。
- `tools/`：6 个工具（`search_code` 待手写）。注册表统一加超时 + `asyncio.Semaphore`
  并发上限，并把任何异常降级成 `ToolResult(ok=False)`。
- `llm/`：`LLMClient` Protocol、`AnthropicLLM`（强制 tool use 拿结构化输出）、
  `ScriptedLLM` 确定性测试替身。
- `agent/`：`AgentState` TypedDict + `operator.add` reducer，6 个节点，重试条件边。
- `evaluation/`：任务成功率、工具选择分布、重试次数、diff 有效性、失败原因。

### Stage A — 业务层（骨架完成，3 处待手写）
- **PostgreSQL 17 + pgvector**（`docker-compose.yml`，端口 5433，复用本机已有镜像）。
- `db/schema.sql`：3 张表 —— `runs`（兼任队列）、`webhook_deliveries`（幂等台账）、
  `approvals`（审批流水）。用到 PG 独有的 ENUM 类型、`jsonb`、原生数组、**部分索引**。
- `domain/status.py`：9 状态的状态机，流转表是唯一真相来源，终态从表推导。
- `db/runs.py`：入队、**领取（租约 + SKIP LOCKED）**、心跳续租、僵尸回收、
  带乐观锁的状态流转。
- `db/deliveries.py`：webhook 幂等去重。
- `db/approvals.py`：审批决策追加写 + 推动状态，与流转同事务。
- `worker/`：事件总线（SSE 用）、Runner（跑一个 run）、Worker（领取循环 +
  进程内并发上限 + 优雅停机 + reaper）。
- `api/`：`POST /runs` 改成**只入队**立刻返回 202；新增 `GET /runs`、
  `POST /runs/{id}/approval`、`GET /runs/{id}/approvals`；`/health` 带队列深度。
- 真实 HTTP 验证通过：入队 → worker 领取 → Agent 修好 → `pending_approval`
  → 批准 → `publishing`；重复批准返回 409；审批流水可查。

- 4 个核心实现全部完成：`search_code`（本人手写）、`can_transition`、
  `CLAIM_DELIVERY_SQL`、`CLAIM_SQL`。**75 passed，ruff 全绿。**

## NOW

Stage A 收尾完成，等待推进 Stage B。

## NEXT

1. **Stage B — GitHub 接入**：webhook 验签、Issue → 入队、开 PR、回写评论。
   幂等台账已经就位，接上去即可。
2. **Stage C — 评测集**：15 个 seeded bug（含跨文件、含故意无解的），出成功率报表。
3. Docker sandbox 替换 `sandbox/local.py`（签名不变）。接了 GitHub 之后优先级上升，
   因为那时要跑陌生仓库的代码。
4. MCP server，把 repo 工具暴露出去。
5. OpenTelemetry：每个节点、每个工具一个 span。
6. README、架构图、简历项目描述。

## BLOCKED

- 无。

## 已知缺口（面试要主动说，别等人问）

- 事件总线是**进程内**的。API 和 worker 拆进程后 SSE 会收不到事件；
  真要拆得换 Redis pub/sub 或 PG 的 `LISTEN/NOTIFY`。业务正确性不受影响 ——
  真相在数据库里，轮询 `GET /runs/{id}` 结果一样。
- 队列空转靠轮询（默认 1s），不是零延迟。`LISTEN/NOTIFY` 可以解决。
- sandbox 是本地子进程，不是容器。隔离靠路径收敛 + 超时，不是内核级。
- API 没有鉴权。
- `publishing → published` 这一步还没有真正的 PR 创建逻辑（Stage B）。
- 评测只有单次运行，还没有任务基准集。
