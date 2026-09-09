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

**Stage B 第一步 — webhook 入口，完成。98 passed，ruff 全绿。**

- `github/webhook.py`：验签 + 事件解析。刻意不碰数据库和 FastAPI，
  输入 `bytes`/`dict`，输出布尔值和 `IssueTrigger` DTO，所以能当纯函数测。
- `POST /webhooks/github`：raw body 验签 → 401 → 幂等登记 → 授权判断 → 入队。
  `claim_delivery` 从"只有测试在用"变成真正接进链路。
- `github_trigger_label` 授权边界：**默认不响应任何 Issue**，打了标签才算授权。
- 密钥未配置时 **fail closed**（拒绝所有），不是"跳过验签"。
- `verify_signature`：`hmac.compare_digest` 常数时间比较；比较前转 bytes，
  否则攻击者塞一个非 ASCII 的签名头就能把 401 变成 500。
- `client` fixture 从 test_api.py 提到 conftest.py，webhook 测试共用。

## NEXT

1. **Stage B 第二步**：clone 目标仓库（现在 webhook 入队时 `repo_path` 还是
   写死的内置样例仓库）。
2. **Stage B 第三步**：`publishing → published` —— 用 PAT push 分支、开 PR、
   回写 Issue 评论（靠 `external_ref` 反查回哪个 Issue）。
3. Docker sandbox：接了陌生仓库之后这条的优先级立刻升到最高。
4. **Stage C — 评测集**：15 个 seeded bug（含跨文件、含故意无解的），出成功率报表。
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
- `publishing → published` 这一步还没有真正的 PR 创建逻辑（Stage B 第三步）。
- webhook 的「登记投递」和「入队 run」**不在同一个事务里**。两者之间崩溃 →
  投递已记账、run 没建成、重投会被判重，事件就丢了。两张表在同一个库，
  技术上完全做得到一个事务，是刻意留的取舍。面试要主动讲这个缺口。
- webhook 入队时 `repo_path` 还写死成内置样例仓库，没有真的 clone 目标仓库。
- 评测只有单次运行，还没有任务基准集。
