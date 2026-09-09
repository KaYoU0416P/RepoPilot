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

**Stage B 第三步 — 发布链路完成。118 passed，ruff 全绿。**
**`Issue → Run → 审批 → PR` 整条链路已闭环。**

- `github/client.py`：GitHub REST 客户端（PAT + httpx，不用 SDK，不碰 OAuth）。
- `publishing/`：`Publisher` 协议 + `GitHubPublisher`（真发）+ `DryRunPublisher`
  （无 token 时降级，`pr_url` 留空表示"没真发"）。和 `llm/` 一个模式。
- `Worker._publish_loop`：和领取循环并排跑的第二个循环，
  `claim_next_publishing` 复用同一套 SKIP LOCKED + 租约。
- **发布幂等**：分支名 `repopilot/run-<id前8位>` 是确定性的，push 重复是 no-op，
  开 PR 前先按 head 分支查已有 PR → 崩在任何一步重试都不会开出两个 PR。
- **失败分两类**：`PublishError`（diff 打不上、4xx）→ 标 failed 不重试；
  网络抖动 / 5xx → 冒出去，租约过期后自动重试。
- **交还租约**：`RELEASE_LEASE` 和状态流转在同一条 UPDATE 里，
  否则批准后 publisher 要干等一个租约周期才接手。
- schema 加了 `branch` / `pr_url` 两列 + `idx_runs_publishable` 部分索引。
  测试库会自愈（conftest 检测到缺列就重建），**开发库要 `make db-reset`**。
- 顺手修了一个循环导入：`api/__init__.py` 原本 re-export `app`，导致
  `worker.bus → api.schemas → api/__init__ → api.app → api.routes → worker`。
  之前靠导入顺序侥幸不崩，加个测试文件就踩到了。

### Stage B 第一步 — webhook 入口

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

1. **Stage C — 评测集**（收益最高）：15 个 seeded bug，含跨文件的、需要读依赖的、
   需要改测试的、**故意无解的**（证明 Agent 会放弃而不是瞎改 —— 这就是 retry
   budget 存在的意义）。产出成功率 / 平均重试 / 平均工具调用 / 失败原因分布。
2. **Stage B 第二步**：clone 目标仓库。现在 webhook 入队时 `repo_path` 还是写死的
   内置样例仓库；发布链路本身已经能处理真实 clone（`GitHubPublisher` 就是
   `git clone repo_path` 起手的），补上 clone 这一步就直接通了。
3. Docker sandbox 替换 `sandbox/local.py`（`run_command` 签名不变）。
   **必须排在第 2 条之后立刻做** —— 一旦 clone 陌生仓库，就是在本机跑别人的测试。
4. MCP server，把 repo 工具暴露出去（要自己实现 Server，不是只接别人的）。
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
- 发布链路**只在本地裸仓库上验证过**（测试用裸仓库当远端，git 那半边是真的，
  GitHub API 那半边是 `httpx.MockTransport`）。没打过真实 GitHub 的 API。
- publisher 的重试**没有次数上限**：`PublishError` 会直接进终态，但如果每次都是
  进程崩在同一个地方（比如 push 卡到超时），租约过期后会一直重来。
  `attempts` 是 Agent 执行的预算，没复用给发布。要补的话得加一个独立计数器。
- webhook 的「登记投递」和「入队 run」**不在同一个事务里**。两者之间崩溃 →
  投递已记账、run 没建成、重投会被判重，事件就丢了。两张表在同一个库，
  技术上完全做得到一个事务，是刻意留的取舍。面试要主动讲这个缺口。
- webhook 入队时 `repo_path` 还写死成内置样例仓库，没有真的 clone 目标仓库。
- 评测只有单次运行，还没有任务基准集。
