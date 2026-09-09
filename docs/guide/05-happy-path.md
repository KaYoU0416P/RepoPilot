# 05 · 主线 Happy Path：一行不落

**这是整份文档的核心。** 从一条 `curl` 开始，走到 PR 被创建，中间每一行代码都讲。

面试时你就照着这条线讲，讲完基本就赢了。

## 我们要追踪的那条命令

```bash
curl -X POST localhost:8000/runs \
  -H 'content-type: application/json' \
  -d '{"task":"Fix divide() so dividing by zero raises ValueError"}'
```

## 全景图（先看一眼，心里有数）

```
      HTTP 线程（事件循环）                       后台 worker（同一个事件循环）
┌────────────────────────────────┐      ┌────────────────────────────────────┐
│ 站1  POST /runs 进门            │      │ 站5  抢令牌 → CLAIM_SQL 领任务      │
│ 站2  校验仓库路径               │      │ 站6  create_task 开一个执行任务      │
│ 站3  INSERT INTO runs           │──┐   │ 站7  设 run_id、建 workspace、开心跳 │
│ 站4  返回 202                   │  │   │ 站8  copytree + git baseline        │
└────────────────────────────────┘  │   │ 站9  建图 → astream                 │
                                    │   │ 站10 analyze                        │
        队列（就是 runs 表本身）  ◀──┘   │ 站11 plan（并发读文件）              │
                                        │ 站12 execute（写文件）               │
                                        │ 站13 run_tests（子进程沙箱）         │
                                        │ 站14 evaluate → 条件边 → finish     │
                                        │ 站15 评估 → pending_approval        │
                                        └────────────────────────────────────┘
                                                        │
站16  SSE 在旁边实时推事件                                │
站17  人工审批 → publishing ────────────────────────────┘
                    │
                    ▼
        ┌──────────────────────────────────────────┐
        │ 站18  发布循环（并排跑的第二条循环）        │
        │   领取 publishing → clone → apply diff    │
        │   → push 分支 → 开 PR → 回写 Issue 评论   │
        │   → published                            │
        └──────────────────────────────────────────┘

站21  另一个入口：GitHub Issue 打标签 → webhook → 汇入站 3
```

---

# 站 0 · 服务是怎么起来的

```bash
make run
# 展开：uv run uvicorn repopilot.api.app:app --reload --port 8000
```

uvicorn 做三件事：
1. `import repopilot.api.app`，找到里面叫 `app` 的变量
2. 起一个 asyncio 事件循环
3. 调用 `lifespan` 的**启动段**，然后开始监听 8000 端口

`app` 从哪来：

```python
# src/repopilot/api/app.py:64
def create_app() -> FastAPI:
    app = FastAPI(title="RepoPilot", version="0.2.0", lifespan=lifespan)
    app.include_router(router)          # 把 routes.py 里所有 @router.xxx 挂上去
    return app

app = create_app()                       # ← uvicorn 要的就是这个
```

**为什么用工厂函数 `create_app()` 而不是直接 `app = FastAPI(...)`**：测试里可以造一个独立的 app 实例，不会和全局的那个互相污染。

启动段逐行（[04-stack.md](04-stack.md) §2.3 讲过，这里只强调顺序）：

```python
setup_logging()                      # 1. 先配日志，否则后面的启动日志都丢了
settings = get_settings()            # 2. 读配置（@lru_cache，全进程只构造一次）
await init_pool(...)                 # 3. 连接池。后面 worker 立刻就要用
app.state.bus = EventBus()           # 4. 事件总线
if settings.enable_worker:
    worker = Worker(settings, app.state.bus)
    app.state.worker_task = asyncio.create_task(worker.run_forever(), name="worker")
                                     # 5. worker 在后台跑起来（不 await，让它自己转）
yield                                # 6. 开始接客
```

**关键**：`create_task` 之后 worker 就在**同一个事件循环里**转起来了。API 处理请求和 worker 领任务是两个协程，在一个线程上交替执行。这就是为什么事件总线可以是进程内的。

此刻 worker 已经在 `while` 里空转，每秒问一次数据库"有活吗"。

---

# 站 1 · 请求进门

```python
# src/repopilot/api/routes.py:69
@router.post("/runs", response_model=CreateRunResponse, status_code=202)
async def create_run(
    body: CreateRunRequest,
    settings: Settings = Depends(get_settings),
) -> CreateRunResponse:
    """只入队，不执行。立刻返回 202，由 worker 异步领取。"""
```

在你的函数第一行执行之前，FastAPI 已经做完了这些：

1. 匹配路由 `POST /runs`
2. 读请求体，`json.loads`
3. 看到 `body: CreateRunRequest` 是 Pydantic 模型 → **用它校验**
   - `task` 少于 3 个字符 → 直接返回 **422**，你的函数根本不会被调用
   - `max_attempts` 不在 1~5 → 422
4. 看到 `settings: Settings = Depends(get_settings)` → 调 `get_settings()` 把结果注入

**所以函数体里的 `body.task` 一定是个合法字符串。** 不用判空、不用判类型 —— 这就是"在边界上校验"的回报。

`status_code=202` 而不是 201：**202 Accepted = "我收到了，还没干"**。这是本项目最重要的一个 HTTP 语义选择，见站 4。

---

# 站 2 · 校验仓库路径

```python
repo_path = _resolve_repo_path(body.repo_path, settings)
```

```python
# src/repopilot/api/routes.py:255
def _resolve_repo_path(raw: str | None, settings: Settings) -> Path:
    """同步函数：两次 stat 调用，别放在 async 处理函数里。"""
    path = Path(raw).expanduser().resolve() if raw else settings.sample_repo
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"仓库路径不存在: {path}")
    return path
```

逐行拆：

| 代码 | 干什么 |
|---|---|
| `def`（不是 `async def`） | **故意的**。里面有两个阻塞的文件系统调用（`resolve` 和 `is_dir` 都要 stat），抽出来变成显式的同步函数，ruff 的 ASYNC240 规则也就不会报警。见 [03-asyncio.md](03-asyncio.md) §6 |
| `Path(raw)` | 字符串 → `Path` 对象。`pathlib` 是 Python 现代的路径操作方式，`/` 运算符可以拼路径：`root / "sub" / "f.py"` |
| `.expanduser()` | 把 `~` 展开成家目录 |
| `.resolve()` | 变成绝对路径，**并且解析掉所有符号链接和 `..`** |
| `if raw else settings.sample_repo` | 三元表达式，Python 写法是 `A if 条件 else B`（顺序和 Java 的 `?:` 不同） |
| `is_dir()` | 不存在或者不是目录 → 400 |

`.resolve()` 这一步是安全相关的：**先把路径规范化，后面所有比较才有意义**。这个思路在 `Workspace.resolve()`（站 8）里会再用一次，而且那次是真正的安全边界。

---

# 站 3 · 落库（真正的入队）

```python
row = await runs_repo.create_run(
    task=body.task,
    repo_path=str(repo_path),
    source="manual",
    max_attempts=body.max_attempts or settings.max_attempts,
)
```

`body.max_attempts or settings.max_attempts`：**`or` 在 Python 里返回的是"第一个为真的值"**，不是布尔。所以这句 = "客户端传了就用它，没传（None）就用配置默认值"。等价于 Java 的 `Optional.ofNullable(x).orElse(default)`，但一个 `or` 就够了。

进到仓储层：

```python
# src/repopilot/db/runs.py:22
async def create_run(
    *,                                          # ← 强制关键字参数，见 02 §10
    task: str,
    repo_path: str,
    source: str = "manual",
    external_ref: str | None = None,
    max_attempts: int = 3,
    conn: asyncpg.Connection | None = None,     # ← 允许外部传连接进来
) -> RunRow:
    sql = """
        INSERT INTO runs (task, repo_path, source, external_ref, max_attempts)
        VALUES ($1, $2, $3::trigger_source, $4, $5)
        RETURNING *
    """
    executor = conn or get_pool()
    record = await executor.fetchrow(sql, task, repo_path, source, external_ref, max_attempts)
    return RunRow.from_record(record)
```

四个细节，每个都有理由：

**① `$3::trigger_source`**
`source` 传过来是普通字符串 `"manual"`，但列的类型是 PG 的自定义枚举 `trigger_source`。`::` 是 PG 的类型转换语法，显式转一下。没有这个转换 asyncpg 会报类型不匹配。

**② `RETURNING *`**
插完直接把整行返回来 —— 包括数据库生成的 `id`（`gen_random_uuid()`）、`created_at`、以及所有默认值。**MySQL 做不到，得再 `SELECT`**。

**③ `executor = conn or get_pool()`**
如果调用方传了连接（说明它想让这次插入参与一个更大的事务），就用那个；否则从池里现借一条（asyncpg 的 Pool 对象本身也有 `fetchrow` 方法，会自动借还）。

**这个模式叫「可选事务参与」**，是没有 ORM 时管理事务边界的标准做法。Java 里 Spring 靠 ThreadLocal 里的连接自动做到这件事，这里是显式传参。

**④ `RunRow.from_record(record)`**
`Record` → `dict` → Pydantic 校验 → 有类型的对象。顺便把 `timestamptz` 转成 `datetime`、文本 `'queued'` 转成 `RunStatus.QUEUED`。

此刻数据库里的这一行：

```
id               = 3f2b...（自动生成）
status           = 'queued'      ← 默认值
task             = 'Fix divide()...'
attempts         = 0
max_attempts     = 3
locked_by        = NULL
lease_expires_at = NULL
created_at       = now()
```

**这一行同时是「业务实体」和「队列消息」。** 记住这句话，面试要用。

---

# 站 4 · 返回 202

```python
return CreateRunResponse(
    run_id=row.id,
    status=row.status,
    events_url=f"/runs/{row.id}/events",
)
```

`response_model=CreateRunResponse` 让 FastAPI 把它序列化成 JSON：

```json
{"run_id":"3f2b...","status":"queued","events_url":"/runs/3f2b.../events","deduplicated":false}
```

**这一站是整个项目最重要的设计决策，必须能讲透：**

> **`POST /runs` 只入队，不执行。**

对比一下如果同步执行会怎样：

| | 同步执行（错误做法） | 入队 + 异步（本项目） |
|---|---|---|
| 响应时间 | 几分钟，HTTP 超时 | 毫秒级 |
| 进程崩了 | **任务凭空消失** | 任务在库里，被别人领走 |
| 并发 100 个请求 | 100 个 Agent 一起跑，机器炸 | 队列里排队，worker 按 `max_concurrent_runs` 慢慢消化 |
| 客户端断线 | 任务白跑 | 无影响 |

**Java 对照**：这就是"接口收到请求 → 落库 → 发 MQ → 返回 202"的标准可靠性套路，只不过这里"落库"和"发 MQ"是**同一个动作**，所以连"双写不一致"都不存在。

`events_url` 是给客户端的**指路牌**：想看进度就去连这个 SSE。这是 REST 里 HATEOAS 的轻量用法。

---

# 站 5 · Worker 领取任务 ⭐

现在切到后台。worker 从站 0 就在这个循环里转：

```python
# src/repopilot/worker/worker.py:41
async def run_forever(self) -> None:
    log.info("worker %s 启动 (并发上限=%s, 租约=%ss)", ...)
    reaper = asyncio.create_task(self._reaper_loop(), name="reaper")
    try:
        while not self._stopping.is_set():
            # 先拿令牌再去数据库领任务：没有空位就不要把任务从队列里捞出来占着
            await self._slots.acquire()
            if self._stopping.is_set():
                self._slots.release()
                break

            row = await runs_repo.claim_next_run(self.worker_id, self.settings.lease_seconds)
            if row is None:
                self._slots.release()
                await self._sleep_or_stop(self.settings.poll_interval_seconds)
                continue

            task = asyncio.create_task(self._guarded_execute(row), name=f"run-{row.id}")
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
    finally:
        reaper.cancel()
        await self._drain()
```

## 5.1 「先拿令牌，再领任务」—— 顺序是关键

```python
await self._slots.acquire()                    # 先
row = await runs_repo.claim_next_run(...)      # 后
```

**如果反过来**（先领任务，再等令牌）：任务已经从队列里捞出来了、状态已经变成 `running` 了、租约已经开始计时了，但它在内存里干等一个空位。这段时间里：

- 别的空闲 worker 看不到它（它已经不是 `queued` 了）
- 租约在白白流逝
- 如果这时候进程崩了，得等一整个租约周期才能被回收

**「不占位」原则**：没有能力处理，就不要把消息从队列里取出来。这在 MQ 里对应的就是 **prefetch / QoS 设置**（RabbitMQ 的 `basic_qos(prefetch_count=N)`）。

**这一条一定要讲，它显示你不只是"会用队列"，而是"知道队列为什么这么用"。**

`acquire()` 之后马上又检查一次 `_stopping`：因为 `acquire()` 可能挂起等待很久，等回来时世界已经变了。**在每个 `await` 之后重新检查前置条件**，是异步编程的基本功。

## 5.2 `CLAIM_SQL` —— 全项目最值钱的 20 行

```sql
UPDATE runs
   SET status           = 'running',
       attempts         = attempts + 1,
       locked_by        = $1,
       lease_expires_at = now() + make_interval(secs => $2),
       started_at       = COALESCE(started_at, now())
 WHERE id = (
     SELECT id
       FROM runs
      WHERE attempts < max_attempts
        AND (status = 'queued'
             OR (status = 'running' AND lease_expires_at < now()))
      ORDER BY created_at
      LIMIT 1
      FOR UPDATE SKIP LOCKED
 )
RETURNING *
```

**从里往外读**，一句一句：

### 内层 SELECT：挑一个能领的

```sql
WHERE attempts < max_attempts
```
重试次数还没用完。用完的不再领，交给 reaper 去标记失败。**这是死信队列的作用。**

```sql
AND (status = 'queued'
     OR (status = 'running' AND lease_expires_at < now()))
```
**两种可领的任务：**
1. `queued` —— 新任务
2. `running` **但租约已经过期** —— 僵尸任务，持有它的 worker 已经死了

第 2 种是整个可靠性机制的核心。**没有它，worker 一崩，任务就永远卡在 `running`。**

```sql
ORDER BY created_at
```
先进先出。这一列上正好有那个部分索引 `idx_runs_claimable`，所以排序不用全表扫。

```sql
LIMIT 1
```
一次只领一个。

```sql
FOR UPDATE SKIP LOCKED
```
- `FOR UPDATE`：把选中的行**锁住**，事务结束前别人改不了
- `SKIP LOCKED`：**遇到已经被别人锁住的行，直接跳过去看下一行**，而不是排队等

三种写法的后果（这个表格背下来）：

| 写法 | 10 个 worker 同时抢 |
|---|---|
| 什么都不加 | 全部读到同一行 → **同一个任务被执行 10 次** |
| `FOR UPDATE` | 排队等锁 → 退化成串行 → **吞吐崩了** |
| `FOR UPDATE SKIP LOCKED` | 各拿各的 → **互不阻塞** |

⚠️ 面试别说错：**`SKIP LOCKED` MySQL 8.0 也有。**

### 外层 UPDATE：领取动作本身

```sql
SET status = 'running'
```
标记为执行中。**"选中"和"标记"在同一条语句里完成，中间没有任何窗口** —— 这是原子性的来源。

```sql
attempts = attempts + 1
```
每领取一次 +1。注意是**领取次数**，不是 Agent 内部的重试次数（那个叫 `retry_count`，是另一回事）。这两个概念别混：
- `attempts` = 这个任务被 worker **领取**了几次（基础设施层重试）
- `retry_count` = Agent 在一次执行里**重跑 execute** 了几次（业务层重试）

```sql
locked_by = $1
```
记下是谁拿着。续租时要用它验证所有权。

```sql
lease_expires_at = now() + make_interval(secs => $2)
```
**租约到期时间**。`make_interval(secs => 120)` 是 PG 造时间间隔的函数，`=>` 是 PG 的命名参数语法（MySQL 没有）。

为什么不用 `now() + interval '120 seconds'`：因为秒数是变量，字面量拼不进去，而 `make_interval` 可以接受参数。

```sql
started_at = COALESCE(started_at, now())
```
`COALESCE(a, b)` = "a 不是 NULL 就用 a，否则用 b"（= Java 的 `a != null ? a : b`）。

效果：**第一次领取时写入当前时间，后续重试时保留原值**。所以 `started_at` 永远是"第一次开始跑"的时间，统计端到端耗时才准。

```sql
RETURNING *
```
领取完直接拿回整行。**MySQL 需要再查一次，而那一次查询和更新之间存在并发窗口。**

### Python 侧

```python
# src/repopilot/db/runs.py:94
record = await get_pool().fetchrow(CLAIM_SQL, worker_id, lease_seconds)
if record is None:
    return None                      # 队列空了
row = RunRow.from_record(record)
log.info("worker=%s 领取 run=%s (第 %s 次尝试)", worker_id, row.id, row.attempts)
return row
```

`log.info("...%s...", a, b)` —— **参数是分开传的，不是 f-string 拼的**。这是 logging 的**惰性格式化**：日志级别没开到 INFO 时，字符串根本不会被拼出来。Java 的 SLF4J `log.info("{} {}", a, b)` 是同一个道理。

## 5.3 队列空了怎么办

```python
if row is None:
    self._slots.release()                                          # 还令牌
    await self._sleep_or_stop(self.settings.poll_interval_seconds)  # 退避 1 秒
    continue
```

`_sleep_or_stop` 的精妙之处见 [03-asyncio.md](03-asyncio.md) §4.3 —— **睡 1 秒，但收到停机信号立刻醒**。

**已知缺口**（面试主动说）：轮询意味着最坏有 1 秒延迟。PG 的 `LISTEN/NOTIFY` 可以做到零延迟推送，但会让代码复杂一截，MVP 阶段接受这个取舍。

---

# 站 6 · 开一个执行任务

```python
task = asyncio.create_task(self._guarded_execute(row), name=f"run-{row.id}")
self._inflight.add(task)
task.add_done_callback(self._inflight.discard)
```

三行，每行都不能删（见 [03-asyncio.md](03-asyncio.md) §3）：

1. `create_task` → 丢进事件循环后台跑，**主循环立刻回去领下一个任务**
2. `_inflight.add` → 拿住强引用，防止 Task 被 GC 掉（事件循环只持弱引用）
3. `add_done_callback(discard)` → 干完自动移除，不泄漏

```python
async def _guarded_execute(self, row) -> None:
    try:
        await self.runner.execute(row, self.worker_id)
    finally:
        self._slots.release()          # ← 无论成功失败，令牌必须还
```

`finally` 里还令牌。**漏掉这个，跑失败几次之后 worker 就永远拿不到令牌，静默死掉。**

---

# 站 7 · Runner 开场

```python
# src/repopilot/worker/runner.py:37
async def execute(self, row: RunRow, worker_id: str) -> None:
    token = run_id_var.set(str(row.id)[:8])
    seq = 0
    started = time.perf_counter()
```

**`run_id_var.set(...)`** —— 从这里开始，这个协程里所有日志自动带上 `[run=3f2b1c8a]`。见 [03-asyncio.md](03-asyncio.md) §8。取前 8 位是因为完整 UUID 太长，日志里读不动。

**`time.perf_counter()`** —— 单调时钟，专门用来测耗时。**不要用 `time.time()`**：那是墙上时钟，会被 NTP 校时、夏令时改动，测出负数都有可能。Java 对照：`System.nanoTime()` vs `currentTimeMillis()`。

## 7.1 `emit` —— 闭包 + `nonlocal`

```python
def emit(event_type: str, node: str | None = None, message: str = "", **data) -> None:
    nonlocal seq
    seq += 1
    self.bus.publish(row.id, RunEvent(run_id=str(row.id), seq=seq, type=event_type,
                                      node=node, message=message, data=data))
```

- **定义在函数里面的函数**（闭包），能直接用外层的 `row`、`self`、`seq`，不用当参数传
- `nonlocal seq` = "我要改的是外层那个 `seq`，不是新建一个局部变量"
  （不写 `nonlocal` 的话 `seq += 1` 会因为"局部变量还没赋值就读"而报错）
- `seq` 递增 = **给事件编号**，客户端可以据此发现丢帧
- `**data` 收集所有额外的关键字参数进 `data` 字段

**为什么 `emit` 是同步函数**：因为 `bus.publish` 内部用 `queue.put_nowait()`（无界队列，永不阻塞）。这样发事件的地方不需要 `await`，代码干净得多。**这是一个 API 设计选择，不是偷懒。**

## 7.2 建 workspace + 开心跳

```python
workspace = self.workspaces.create(Path(row.repo_path), run_id=str(row.id))
emit("run_started", message=f"workspace 就绪: {workspace.root.name}")

heartbeat_task = asyncio.create_task(self._heartbeat_loop(row.id, worker_id), name=f"hb-{row.id}")
```

心跳循环：

```python
async def _heartbeat_loop(self, run_id, worker_id: str) -> None:
    interval = self.settings.lease_seconds / 3        # 120 / 3 = 40 秒
    while True:
        await asyncio.sleep(interval)
        alive = await runs_repo.heartbeat(run_id, worker_id, self.settings.lease_seconds)
        if not alive:
            log.warning("run=%s 租约已丢失，停止续租", run_id)
            return
```

**为什么是 `lease_seconds / 3`**：租约 120 秒，每 40 秒续一次 —— 允许**连续丢两次心跳**（网络抖动、GC 停顿）才会真的过期。这是心跳间隔的经典取值（Raft、Kafka 都是这个思路：`超时 ≈ 3 × 心跳间隔`）。

续租 SQL：

```python
# src/repopilot/db/runs.py:108
sql = """
    UPDATE runs
       SET lease_expires_at = now() + make_interval(secs => $3)
     WHERE id = $1 AND locked_by = $2 AND status = 'running'
"""
result = await get_pool().execute(sql, run_id, worker_id, lease_seconds)
return result.endswith(" 1")
```

**`AND locked_by = $2` 这个条件是灵魂。**

场景：我这个 worker 卡住了 3 分钟（GC 停顿 / 机器挂起），租约过期，任务被 worker-B 领走。现在我醒了，想续租。

- **没有 `locked_by` 条件**：我把 `lease_expires_at` 又往后推了，于是**两个 worker 同时在跑同一个 run** —— 两份文件写入、两个 PR。
- **有这个条件**：`locked_by` 已经是 worker-B 了，我的 UPDATE 匹配 0 行 → `execute` 返回 `"UPDATE 0"` → `.endswith(" 1")` 是 False → **我知道自己失去了所有权，停止续租**。

**Java 对照**：Redisson 看门狗续期时也必须验证锁的持有者（value 是不是自己的 UUID），一模一样的道理。**这是分布式锁最经典的坑。**

---

# 站 8 · Workspace：三层隔离的第一层和第三层

```python
# src/repopilot/workspace/manager.py:64
def create(self, source_repo: Path, run_id: str | None = None) -> Workspace:
    run_id = run_id or uuid.uuid4().hex[:12]
    target = self.root / run_id                        # Path 支持 / 拼接
    if target.exists():
        shutil.rmtree(target)                          # 重试时清掉上次的残留
    shutil.copytree(source_repo, target, ignore=shutil.ignore_patterns(*_IGNORED))
    ws = Workspace(run_id=run_id, root=target)
    self._git_init(ws)
    return ws
```

## 8.1 `copytree`：整个仓库复制一份

```python
_IGNORED = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules", ".ruff_cache"}
```

**Agent 全程只操作副本，真实仓库一个字节都不会被动。** 这是"炸弹半径控制" —— 就算 Agent 把代码写成一坨屎，`rm -rf .workspaces/<id>` 就全没了。

排除 `.git` 有两个原因：一是不复制历史节省时间和空间，二是**下面要重新 `git init` 建一个干净的基线**。

## 8.2 `_git_init`：基线提交

```python
@staticmethod
def _git_init(ws: Workspace) -> None:
    """Baseline commit so `git diff` later shows exactly what the agent changed."""
    env = {
        "GIT_AUTHOR_NAME": "repopilot",
        "GIT_AUTHOR_EMAIL": "agent@repopilot.local",
        "GIT_COMMITTER_NAME": "repopilot",
        "GIT_COMMITTER_EMAIL": "agent@repopilot.local",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "baseline", "--no-gpg-sign"]):
        subprocess.run(cmd, cwd=ws.root, env=env, check=True, capture_output=True)
```

**为什么要这一步**：先给"Agent 动手之前"的状态打一个提交，之后 `git diff` 出来的**就精确等于 Agent 改了什么**。不用自己写 diff 算法，白嫖 git。

细节：

- **显式传 `env`** 而不是继承环境：如果用户的 git 配了 GPG 签名、配了 commit hook、配了奇怪的 `user.email`，这里会失败。显式给一套最小环境，**让行为在任何机器上都一样**。
- **`--no-gpg-sign`** 双保险。
- **`PATH` 得手动带上**，因为 `env=` 是**完全替换**不是追加，不给 PATH 就找不到 `git` 命令。
- 这里用同步的 `subprocess.run` 是因为 `_git_init` 本身就是同步函数，而且三条 git 命令是毫秒级的。

## 8.3 `Workspace.resolve()` —— 安全核心

```python
# src/repopilot/workspace/manager.py:29
def resolve(self, relative_path: str) -> Path:
    """Map an agent-supplied path to a real path, or refuse.

    Blocks absolute paths, `..` traversal and symlinks that point outside root.
    """
    candidate = (self.root / relative_path).resolve()
    root = self.root.resolve()
    if candidate != root and root not in candidate.parents:
        raise PathEscapeError(f"path escapes workspace: {relative_path!r}")
    return candidate
```

**每一个由大模型产生的路径，都必须经过这四行。**

拆解：

**① `(self.root / relative_path)`**
`Path` 的 `/` 运算符。⚠️ 有个反直觉的规则：**如果右边是绝对路径，结果就是右边**。

```python
Path("/ws/abc") / "sub/f.py"        # /ws/abc/sub/f.py
Path("/ws/abc") / "/etc/passwd"     # /etc/passwd   ← 左边被丢掉了！
```

所以**光靠拼接挡不住绝对路径攻击**，必须有下面的检查。

**② `.resolve()`**
真正的安全关键。它把路径变成规范的绝对路径，**并且真的跟着符号链接走**：
- `ws/../../../etc/passwd` → `/etc/passwd`
- `ws/link_to_home` （一个指向 `/Users/kayou` 的软链）→ `/Users/kayou`

**攻击者的花招在这一步全部现原形。**

**③ `root not in candidate.parents`**
`.parents` 是这个路径的所有祖先目录。`root` 必须在里面，否则就是逃出去了。

**④ `candidate != root and ...`**
特例：路径正好就是 workspace 根目录本身。这时 `root not in root.parents`（自己不是自己的祖先），会误判成逃逸。加这个条件放行。

**能挡住什么**：

| 攻击 | 结果 |
|---|---|
| `../../../../etc/passwd` | `.resolve()` 展开 → 不在 root 下 → 拒绝 |
| `/etc/passwd` | 拼接后变成 `/etc/passwd` → 不在 root 下 → 拒绝 |
| 指向外面的软链接 | `.resolve()` 跟过去 → 不在 root 下 → 拒绝 |
| `sub/../sub/f.py` | 展开后还在 root 下 → **放行**（这是合法路径） |

抛出的 `PathEscapeError` 在 `ToolRegistry.call` 被抓住，转成 `ToolResult(ok=False, error="blocked by sandbox: ...")` —— **模型看到的是一条错误信息，不是一个崩溃。它还能继续工作，只是这一步被拒绝了。**

**面试口径**：
> "Sandbox 不是一个功能，是三个不同的风险配三个不同的措施：写错路径 → `Workspace.resolve()` 路径收敛；生成的代码不终止 → 墙钟超时 + 杀进程组；改坏真实仓库 → 全程操作 copytree 出来的副本。
> 另外我**刻意没有提供通用 shell 工具** —— 有了 shell，上面三条全是装饰品。唯一的执行类工具是 `run_tests`，命令行是写死的。"

---

# 站 9 · 建图、跑图

```python
graph = build_graph(build_llm(), build_registry(), workspace)
state = initial_state(str(row.id), row.task, row.repo_path, self.settings.max_retries)

async for chunk in graph.astream(state, stream_mode="updates"):
    for node_name, partial in chunk.items():
        state = {**state, **_merge(state, partial)}
        emit("node_completed", node=node_name,
             message=(partial.get("step_log") or [node_name])[-1],
             verdict=state.get("verdict"), retry_count=state.get("retry_count", 0))
```

三个 `build_xxx` 都是**工厂函数**，把"选哪个实现"这件事收在一个地方：

```python
# src/repopilot/llm/__init__.py:6
def build_llm() -> LLMClient:
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        from repopilot.llm.anthropic_client import AnthropicLLM     # ← 函数内 import
        return AnthropicLLM(api_key=..., model=..., max_tokens=...)
    return ScriptedLLM()
```

⚠️ **注意那个函数内部的 `import`**：这样在没配 API key 的场景下，`anthropic` 这个包**根本不会被加载**。好处是启动更快，而且万一那个包有问题也不影响测试路径。这叫**延迟导入**，是 Python 的常见手法。

`message=(partial.get("step_log") or [node_name])[-1]`：
拿这个节点新加的最后一条日志当事件消息；没有的话就用节点名。**一行里同时处理了"键不存在"和"值是空列表"两种情况。**

---

# 站 10 · `analyze` 节点

```python
# src/repopilot/agent/nodes.py:48
async def analyze(self, state: AgentState) -> dict[str, Any]:
    """Look at the repo tree, ask the LLM what is relevant."""
    listing = await self.registry.call("list_files", self.ws, glob="**/*.py")
    analysis = await self.llm.structured(
        system=prompts.SYSTEM,
        user=prompts.ANALYZE_USER.format(task=state["task"], tree=listing.content),
        schema=Analysis,
    )
    return {
        "analysis": analysis,
        "tool_calls": [_record(listing, glob="**/*.py")],
        "step_log": [f"analyze: {len(analysis.relevant_files)} candidate files"],
    }
```

流程：**列文件 → 连同任务描述一起发给模型 → 拿回一个 `Analysis` 对象**。

`Analysis` 里有 `relevant_files`（该读哪些文件）和 `search_queries`（该搜什么），供下一个节点用。

**返回的是部分 state**，三个键：
- `analysis` → 覆盖式写入
- `tool_calls` → 有 reducer，**追加**
- `step_log` → 有 reducer，**追加**

`_record()` 把 `ToolResult` 压扁成一条遥测记录：

```python
def _record(result: ToolResult, **args: Any) -> ToolCallRecord:
    preview = json.dumps(args, default=str)[:120]
    return ToolCallRecord(tool=result.tool, ok=result.ok, duration_ms=result.duration_ms,
                          args_preview=preview, error=result.error)
```

`json.dumps(args, default=str)` 里的 `default=str` = "遇到不会序列化的对象就 `str()` 它"，**保证这行永远不会抛异常**。遥测代码炸掉主流程是最冤的死法。

`[:120]` 截断 —— 参数预览而已，不需要完整内容。

---

# 站 11 · `plan` 节点：并发取证

```python
# src/repopilot/agent/nodes.py:63
async def plan(self, state: AgentState) -> dict[str, Any]:
    """Gather evidence (concurrently), then commit to one strategy."""
    analysis = state["analysis"]
    assert analysis is not None

    read_jobs = [self.registry.call("read_file", self.ws, path=p)
                 for p in analysis.relevant_files[:5]]
    search_jobs = [self.registry.call("search_code", self.ws, pattern=q)
                   for q in analysis.search_queries[:3]]
    results = await asyncio.gather(*read_jobs, *search_jobs)

    evidence = "\n\n".join(f"### {r.meta.get('path', r.tool)}\n{r.content}"
                           for r in results if r.ok)
    records = [_record(r) for r in results]

    plan = await self.llm.structured(
        system=prompts.SYSTEM,
        user=prompts.PLAN_USER.format(task=state["task"], reasoning=analysis.reasoning,
                                      evidence=evidence or "(no evidence gathered)"),
        schema=Plan,
    )
    return {"plan": plan, "tool_calls": records, "step_log": [f"plan: {plan.summary}"]}
```

## 关键点逐条

**`assert analysis is not None`**
给类型检查器看的（`AgentState` 里这个字段是 `Analysis | None`）。运行时如果真是 None 会抛 `AssertionError`。这里合理，因为 `analyze` 一定在 `plan` 之前跑过 —— 是 None 就说明图坏了，应该炸。

**`[:5]` 和 `[:3]`**
预算控制。**提示词里写了"最多 5 个"不代表模型会听。**

**`asyncio.gather(*read_jobs, *search_jobs)`**
8 个 I/O 并发跑。但 `ToolRegistry` 里的 Semaphore（4）会限制实际同时在跑的数量。**两层限流在这里第一次真正生效。**

回顾 [03-asyncio.md](03-asyncio.md) §4.1：`self.registry.call(...)` 只是造协程对象，**列表推导式跑完的时候一个文件都还没读**，`gather` 才是发令枪。

**`for r in results if r.ok`**
只把成功的结果拼成证据。失败的工具调用**不会污染提示词**，但会在 `records` 里被记录下来（评估时能看到）。

**`r.meta.get('path', r.tool)`**
`read_file` 的 meta 里有 `path`，`search_code` 没有 —— 就退回用工具名当标题。一行处理两种情况。

**`evidence or "(no evidence gathered)"`**
空字符串是假值，所以这句 = "有证据就用证据，一条都没有就明说"。**给模型一个明确的"没有信息"，比给它一个空字符串强得多**（空字符串会让模型以为自己漏看了）。

---

# 站 12 · `execute` 节点：真正动手改代码

```python
# src/repopilot/agent/nodes.py:100
async def execute(self, state: AgentState) -> dict[str, Any]:
    plan = state["plan"]
    assert plan is not None
    attempt = state["retry_count"] + 1

    analysis = state.get("analysis")
    targets = plan.files_to_edit[:5] or (analysis.relevant_files[:2] if analysis else [])
    reads = await asyncio.gather(
        *(self.registry.call("read_file", self.ws, path=p) for p in targets))
    current = "\n\n".join(
        f"### {r.meta['path']}\n```\n{_strip_line_numbers(r.content)}\n```"
        for r in reads if r.ok)
```

**`plan.files_to_edit[:5] or (analysis.relevant_files[:2] if analysis else [])`**
读作："用计划里要改的文件；**如果计划里一个都没有**（空列表 = 假值），退而求其次用分析阶段找到的前两个文件；连分析都没有就空着"。

**三层降级写在一行里。** 这是 `or` 的惯用法，Java 得写成嵌套三元或者 if-else。

## 12.1 `_strip_line_numbers` —— 一个真实的踩坑

`read_file` 返回的内容是**带行号**的：

```python
# src/repopilot/tools/fs_tools.py:42
numbered = "\n".join(f"{i:>4} | {line}" for i, line in enumerate(text.splitlines(), start=1))
```

```
   1 | def divide(a, b):
   2 |     return a / b
```

行号是给模型**定位**用的（"第 2 行有问题"）。但 `execute` 要模型**重写整个文件**，如果把带行号的内容喂进去，模型很可能连行号一起写回文件。所以：

```python
def _strip_line_numbers(text: str) -> str:
    """read_file returns '  12 | code'. The LLM must rewrite raw content, not that."""
    out = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" | ")
        out.append(tail if sep and head.strip().isdigit() else line)
    return "\n".join(out)
```

`partition(" | ")` 返回三元组 `(前, 分隔符, 后)`。判断条件是**两个**：
- `sep` 非空 → 确实找到了分隔符
- `head.strip().isdigit()` → 前面那段确实是纯数字

**两个条件缺一不可**：源代码里本来就可能有 `" | "`（比如 `a | b` 的位运算）。只判断分隔符会误伤真实代码。

**这是一条"看起来无关紧要，实际上不写就出 bug"的代码。** 面试讲这种细节比讲架构更有说服力。

## 12.2 重试反馈：唯一让第 N+1 次和第 N 次不同的东西

```python
feedback = ""
last = state.get("test_result")
if last is not None and not last.passed:
    feedback = prompts.RETRY_FEEDBACK.format(
        attempt=attempt,
        max_attempts=state["max_retries"] + 1,
        test_output=last.output[-3000:],
    )
```

**`last.output[-3000:]` 取的是最后 3000 个字符，不是前 3000。** 因为 pytest 的错误信息（assertion 详情、traceback）在输出的**末尾**，开头全是 collecting 之类的噪音。

`docstring` 里写得很清楚：
> "On a retry the failing test output is appended to the prompt, which is the only thing that makes attempt N+1 different from attempt N."

**这句话面试可以直接引用**：重试不是"再试一次运气"，是**把失败信息喂回去**。没有这个反馈，重试三次只会得到三个一模一样的错误答案。

## 12.3 调模型 + 写文件

```python
try:
    edit_set = await self.llm.structured(system=..., user=..., schema=EditSet)
except LLMError as exc:
    return {"errors": [f"execute: {exc}"], "tool_calls": [_record(r) for r in reads],
            "step_log": ["execute: LLM failed to produce edits"]}
```

模型输出不合 schema → **不抛异常，返回一个带 error 的部分 state**。图继续往下走到 `run_tests`（测试会失败）→ `evaluate`（判定 retry）→ 再来一次。**一次模型抽风不会让整个 run 崩掉。**

```python
writes = []
for edit in edit_set.edits[:5]:
    writes.append(await self.registry.call("write_file", self.ws,
                                           path=edit.path, content=edit.content))
```

⚠️ **注意：写文件是串行的 `for` 循环，不是 `gather`。**

读文件用并发，写文件用串行 —— 这是**故意的**：
- 读是幂等的、无副作用的，并发没风险
- 写有副作用。并发写如果两个 edit 指向同一个文件，结果不确定
- 而且写文件很快，并发收益接近 0

**面试口径**：**"读并发，写串行 —— 因为副作用需要确定的顺序。"** 这一句就够了。

```python
changed = [e.path for e, w in zip(edit_set.edits, writes, strict=False) if w.ok]
return {
    "edits": edit_set,
    "files_changed": changed,
    "tool_calls": [_record(r) for r in reads] + [_record(w) for w in writes],
    "errors": [w.error for w in writes if w.error],
    "step_log": [f"execute attempt {attempt}: wrote {len(changed)} file(s)"],
}
```

`zip` 把"模型要求的编辑"和"实际写入的结果"配对，只有 `w.ok` 的才算真的改了。**不信任"我发了写入请求"，只认"写入返回成功"。**

## 12.4 工具调用的护栏（`write_file` 这一路走完）

`registry.call` 内部（[02-syntax.md](02-syntax.md) §13 讲过异常部分，这里看完整流程）：

```python
# src/repopilot/tools/base.py:70
async def call(self, name, workspace, *, timeout=None, **kwargs) -> ToolResult:
    spec = self._tools.get(name)
    if spec is None:
        return ToolResult(tool=name, ok=False, error=f"unknown tool: {name}")

    started = time.perf_counter()
    async with self._semaphore:                          # ① 限流
        try:
            result = await asyncio.wait_for(              # ② 超时
                spec.fn(workspace, **kwargs), timeout=timeout or self._timeout)
        except TimeoutError:
            result = ToolResult(tool=name, ok=False, error="tool timed out")
        except PathEscapeError as exc:                   # ③ 沙箱拦截
            result = ToolResult(tool=name, ok=False, error=f"blocked by sandbox: {exc}")
        except NotImplementedError as exc:
            result = ToolResult(tool=name, ok=False, error=f"not implemented: {exc}")
        except TypeError as exc:                         # ④ 模型传错参数
            result = ToolResult(tool=name, ok=False, error=f"bad arguments: {exc}")
        except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the run
            result = ToolResult(tool=name, ok=False, error=f"{type(exc).__name__}: {exc}")

    result.duration_ms = int((time.perf_counter() - started) * 1000)   # ⑤ 计时
    log.info("tool=%s ok=%s %dms", name, result.ok, result.duration_ms)
    return result
```

**五个横切关注点，全在一个地方实现，所有工具自动继承。**

`except TypeError` 单独抓很重要：模型可能调 `write_file(file="x")`（参数名写错了），Python 会抛 `TypeError: unexpected keyword argument`。**转成一条清晰的 "bad arguments" 错误信息回给模型，它下一轮就能改正。**

`duration_ms` 在 `async with` **外面**赋值 —— 所以它统计的是**含排队等待**的总耗时，不只是执行时间。想区分的话得记两个时间点。这是个可以主动提的小细节。

**Java 对照**：这就是一个 AOP 切面（`@Around`），做限流 + 超时 + 异常转换 + 埋点。只是这里是 30 行显式代码，没有代理和反射。

---

# 站 13 · `run_tests` 节点：判定真伪的地方

```python
# src/repopilot/agent/nodes.py:167
async def run_tests(self, state: AgentState) -> dict[str, Any]:
    result = await self.registry.call("run_tests", self.ws, timeout=None)
    outcome = TestOutcome(
        passed=bool(result.meta.get("passed")),
        exit_code=result.meta.get("exit_code"),
        timed_out=bool(result.meta.get("timed_out")),
        summary=_last_line(result.content),
        output=result.content,
    )
    return {"test_result": outcome, "tool_calls": [_record(result)],
            "step_log": [f"run_tests: {'PASS' if outcome.passed else 'FAIL'} ({outcome.summary})"]}
```

`timeout=None` → 用注册表的默认值（20 秒）。但**工具内部还有自己的 60 秒超时**（`test_timeout_seconds`）……

⚠️ **这里其实有个真实的不一致**：注册表的 20 秒会比工具内部的 60 秒先触发。跑得慢的测试套件会被外层先掐掉，报 "tool timed out" 而不是 "tests timed out"。对当前这个 3 个测试的样例仓库没影响，但**接真实仓库前必须修**（让 `run_tests` 显式传 `timeout=settings.test_timeout_seconds + 5`）。

**这种"我知道但还没修"的问题，面试主动说出来是加分项**，被问出来才承认是减分项。

## 13.1 工具本体

```python
# src/repopilot/tools/test_runner.py:20
async def run_tests(workspace: Workspace, target: str = "") -> ToolResult:
    settings = get_settings()
    command = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    if target:
        command.append(workspace.relative(workspace.resolve(target)))
```

- **`sys.executable`** = 当前正在跑的那个 Python 解释器的绝对路径。用它而不是字面量 `"python"`，保证跑的是**虚拟环境里的** Python，不会串到系统 Python 上。
- **`-p no:cacheprovider`** = 禁用 pytest 缓存，别在 workspace 里拉 `.pytest_cache` 出来污染 diff。
- **`command` 是一个列表，不是字符串** —— 见下面 §13.2。
- `workspace.relative(workspace.resolve(target))` = **先收敛再转回相对路径**。哪怕 target 是模型给的恶意路径，`resolve` 会先拦住。

```python
    result = await run_command(command, cwd=workspace.root,
                               timeout=settings.test_timeout_seconds,
                               env={"PYTHONDONTWRITEBYTECODE": "1"})
    output = f"{result.stdout}\n{result.stderr}".strip()

    if result.timed_out:
        return ToolResult(tool="run_tests", ok=False,
                          error=f"tests timed out after {settings.test_timeout_seconds}s",
                          content=output, meta={"timed_out": True, "passed": False})

    counts = {kind: int(n) for n, kind in _SUMMARY.findall(output)}
    return ToolResult(
        tool="run_tests",
        ok=True,          # the tool ran; whether tests passed is in meta
        content=output,
        meta={"passed": result.exit_code == 0, "exit_code": result.exit_code,
              "counts": counts, "duration_ms": result.duration_ms},
    )
```

⭐ **`ok=True` 但测试可能没过。** 这个区分非常重要：

- `ok` = **工具本身有没有正常工作**（pytest 跑起来了、拿到输出了）
- `meta["passed"]` = **测试有没有通过**（业务结果）

测试失败是**正常的业务结果**，不是工具故障。混淆这两者的话，"测试没过"会被当成"工具坏了"，重试逻辑就全乱了。

`PYTHONDONTWRITEBYTECODE=1` = 别生成 `__pycache__`，同样是为了不污染 diff。

`_SUMMARY.findall(output)` 用正则 `(\d+) (passed|failed|error|errors)` 从 `"2 passed, 1 failed in 0.3s"` 里抠出 `{"passed": 2, "failed": 1}`。

## 13.2 沙箱执行（[03-asyncio.md](03-asyncio.md) §7 讲过机制，这里补安全含义）

```python
proc = await asyncio.create_subprocess_exec(*command, cwd=str(cwd), ...,
                                            start_new_session=True)
```

**`create_subprocess_exec` 而不是 `create_subprocess_shell`** —— 这是一条安全边界：

| | 有 shell | 无 shell（本项目） |
|---|---|---|
| 传参方式 | 一个字符串，shell 解析 | 一个**列表**，直接 execve |
| `; rm -rf /` | **会被执行** | 只是一个普通的字符串参数 |
| 通配符、管道、变量展开 | 会 | 不会 |

**命令注入的整类问题，靠"不给 shell"从根上消掉了。**

超时处理：`wait_for` 超时 → `os.killpg` 杀整个进程组 → 再收 5 秒输出 → 返回 `timed_out=True`。

输出截断也有讲究：

```python
def _truncate(raw: bytes, limit: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - limit} chars truncated]...\n{text[-half:]}"
```

**掐中间，保留头尾。** 因为头部有"跑了什么"，尾部有"错在哪"，中间的重复输出最没价值。`errors="replace"` 保证乱码不会让整个函数抛异常。

---

# 站 14 · `evaluate` + 条件边 + `finish`

```python
# src/repopilot/agent/nodes.py:185
async def evaluate(self, state: AgentState) -> dict[str, Any]:
    """Decide the verdict. The conditional edge only *reads* this, never recomputes it."""
    test = state.get("test_result")
    if test is not None and test.passed and state.get("files_changed"):
        return {"verdict": "success", "step_log": ["evaluate: success"]}

    if state["retry_count"] < state["max_retries"]:
        n = state["retry_count"] + 1
        return {"verdict": "retry", "retry_count": n,
                "step_log": [f"evaluate: retry {n}/{state['max_retries']}"]}

    return {"verdict": "failed", "step_log": ["evaluate: budget exhausted"]}
```

**成功的定义有两个条件，`and` 连着：**
1. 测试通过
2. **确实改了文件**

第 2 条防的是一种很隐蔽的假成功：**测试本来就是通过的**（模型什么都没干，或者任务本身描述有误）。只看测试结果的话，"什么都不做"是最容易通过的策略。

**面试口径**：
> "判定成功不能只看测试通过 —— 那样'什么都不做'就是最优策略。必须同时满足'测试通过'和'确实产生了变更'。评估层还会再加一条'diff 非空'，三重确认。"

`retry_count` 在这里 +1，然后条件边把流程送回 `execute`：

```python
def route_after_evaluate(state: AgentState) -> str:
    return "execute" if state.get("verdict") == "retry" else "finish"
```

**路由函数是纯的**：判断逻辑在 `evaluate` 里做完了，这里只读结果。所以能单独当普通函数测试。

`finish` 节点：

```python
async def finish(self, state: AgentState) -> dict[str, Any]:
    diff = await self.registry.call("git_diff", self.ws)
    ...
    lines = [f"verdict: {verdict}",
             f"files changed: {', '.join(state.get('files_changed') or []) or 'none'}",
             f"tests: {'passed' if test and test.passed else 'failed'}" + (...),
             f"attempts: {state['retry_count'] + 1}",
             f"tool calls: {len(state.get('tool_calls') or [])}"]
    return {"diff": diff.content, "final_report": "\n".join(lines),
            "tool_calls": [_record(diff)], "step_log": ["finish"]}
```

`git_diff` 工具：

```python
# src/repopilot/tools/git_tools.py:15
await run_command(["git", "add", "-A"], cwd=workspace.root, timeout=...)
result = await run_command(["git", "diff", "--cached", "--stat", "--patch"], ...)
```

**先 `git add -A` 再 `git diff --cached`** —— 因为**新创建的文件在没 add 之前是 untracked 的，`git diff` 看不到它们**。这是一个很容易漏的点。

`--stat --patch` = 又要统计摘要（改了几个文件、几行）又要完整补丁。

`', '.join(...) or 'none'`：列表空 → join 出空字符串 → 是假值 → 显示 "none"。

---

# 站 15 · 评估 + 落库

回到 Runner：

```python
report = evaluate_run(state)
duration_ms = int((time.perf_counter() - started) * 1000)

if report.task_success:
    await runs_repo.transition(row.id, RunStatus.PENDING_APPROVAL,
        verdict=state.get("verdict"), diff=state.get("diff"),
        final_report=state.get("final_report"), evaluation=report.model_dump(),
        step_log=state.get("step_log") or [], files_changed=state.get("files_changed") or [],
        retry_count=state.get("retry_count", 0))
    emit("run_finished", message="Agent 完成，等待人工审批", ...)
```

## 15.1 评估：不只看结果，也看轨迹

```python
# src/repopilot/evaluation/metrics.py:36
def evaluate_run(state: AgentState) -> RunEvaluation:
    ...
    selection: dict[str, int] = {}
    for call in calls:
        selection[call.tool] = selection.get(call.tool, 0) + 1      # 统计每个工具用了几次

    diff_valid = bool(diff.strip()) and diff.strip() != "(no changes)"
    tests_passed = bool(test and test.passed)
    success = state.get("verdict") == "success" and tests_passed and diff_valid
```

产出长这样：

```json
{
  "task_success": true, "tests_passed": true, "retry_count": 0,
  "tool_calls_total": 7, "tool_calls_failed": 0,
  "tool_selection": {"list_files":1,"read_file":2,"search_code":1,"write_file":1,"run_tests":1,"git_diff":1},
  "diff_valid": true, "files_changed": 1, "failure_reason": "none"
}
```

**为什么要 `tool_selection`（工具选择分布）**：它是判断 Agent 好坏的一个真实信号。一个只会 `read_file` 从不 `search_code` 的 Agent，在大仓库上一定会失败。**只看最终成功率看不出这个。**

`_failure_reason` 把失败归到六类之一（`no_edits_produced` / `tests_failed` / `tests_timed_out` / `tool_errors` / `budget_exhausted` / `none`）。有了分类，跑完基准集就能出一张"失败原因分布"表 —— **这才叫评测，不是"感觉还行"**。

⚠️ 注意 `success` 这里是**第三重确认**：`verdict == "success"`（节点判的）**且** 测试过 **且** diff 非空。

## 15.2 ⭐ 成功 ≠ published

```python
if report.task_success:
    await runs_repo.transition(row.id, RunStatus.PENDING_APPROVAL, ...)
```

**Agent 干得再好，也只能到 `pending_approval`。**

而且这不是靠"我记得在这里写 PENDING_APPROVAL"来保证的 —— 是**状态机在结构上就不允许**：

```python
# src/repopilot/domain/status.py:35
RunStatus.RUNNING: frozenset({
    RunStatus.PENDING_APPROVAL,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.QUEUED,
}),
```

`RUNNING` 的出边里**没有 `PUBLISHED`**。就算有人手滑写了 `transition(id, RunStatus.PUBLISHED)`，`assert_transition` 会抛 `InvalidTransition`。

**面试口径（这句直接背下来）**：
> "`running` 到 `published` 没有直达的边，必须经过 `pending_approval`。**这一条约束就是审批闸门的全部实现** —— 不是靠代码里记得检查，是靠状态机在结构上不允许。"

## 15.3 `transition()` 逐行

```python
# src/repopilot/db/runs.py:136
async def transition(run_id, target, *, expected=None, **fields) -> RunRow:
    async with transaction() as conn:
        current_record = await conn.fetchrow(
            "SELECT status FROM runs WHERE id = $1 FOR UPDATE", run_id)
        if current_record is None:
            raise LookupError(f"run 不存在: {run_id}")

        current = RunStatus(current_record["status"])
        if expected is not None and current != expected:
            raise ValueError(f"run {run_id} 期望是 {expected}，实际是 {current}")

        assert_transition(current, target)                     # ← 第 1 层：应用层守卫

        sets = ["status = $2::run_status"]
        args: list[Any] = [run_id, target.value]
        for i, (column, value) in enumerate(fields.items(), start=3):
            sets.append(f"{column} = ${i}")
            args.append(value)

        guard = len(args) + 1
        record = await conn.fetchrow(
            f"UPDATE runs SET {', '.join(sets)} "
            f"WHERE id = $1 AND status = ${guard}::run_status RETURNING *",   # ← 第 2 层：乐观锁
            *args, current.value)
        if record is None:
            raise RuntimeError(f"run {run_id} 状态被并发修改了")

        log.info("run=%s %s → %s", run_id, current, target)
        return RunRow.from_record(record)
```

### 三道防线

**① `SELECT ... FOR UPDATE`（悲观锁）**
读当前状态的同时把行锁住，同一时刻只有一个事务能走到后面。

**② `assert_transition(current, target)`（应用层守卫）**
查那张 `TRANSITIONS` 表，非法就抛 `InvalidTransition`。**挡的是业务上不该发生的流转。**

**③ `WHERE id = $1 AND status = <当前状态>`（乐观锁）**
UPDATE 的时候再确认一次状态没变。匹配 0 行 → `fetchrow` 返回 `None` → 抛"状态被并发修改了"。

**有了 ① 为什么还要 ③**：纵深防御。如果哪天有人重构掉了那个 `FOR UPDATE`（或者换了隔离级别），③ 还在。而且 ③ 的代价是零 —— 反正都要写 WHERE。

**Java 对照**：③ 就是 JPA 的 `@Version` 乐观锁，只不过这里**用 `status` 本身当版本号**。

### 为什么自转（X → X）是非法的

`TRANSITIONS[RunStatus.RUNNING]` 里不包含 `RUNNING`。

因为③用 `status` 当版本号。如果允许 `running → running`，那这个"版本号"就不会变，乐观锁就失去意义了。**这是一个从实现约束反推出来的业务规则**，值得讲。

### 动态 SQL 的安全边界

```python
sets.append(f"{column} = ${i}")     # 列名拼字符串
args.append(value)                   # 值走 $N 占位符
```

**列名必须拼**（SQL 不允许参数化标识符），**值绝对不能拼**。

这里安全的理由是：`fields` 的键全部来自我们自己代码里的关键字参数名（`verdict=`、`diff=`、`evaluation=`……），**不可能来自用户输入**。这个边界要能说清楚，否则面试官会追问 SQL 注入。

`guard = len(args) + 1` —— 算出乐观锁条件该用第几号占位符。假设有 7 个字段，`args` 是 `[run_id, target, f1..f7]` 共 9 个，那 guard 就是 `$10`，最后 `*args, current.value` 把 `current.value` 作为第 10 个参数传进去。

### 存进去的东西

`evaluation=report.model_dump()` → Python dict → 编解码器 `json.dumps` → PG 的 `jsonb`
`step_log=[...]` → list → jsonb
`files_changed=[...]` → list → PG 原生 `text[]`

三个 PG 特性在这一句里全用上了。

---

# 站 16 · SSE：旁路的实时通道

这条线和主线**并行**发生。

**发布侧**（Runner 每个节点完成时）：

```python
emit("node_completed", node=node_name, message=..., verdict=..., retry_count=...)
   ↓
bus.publish(run_id, RunEvent(...))
   ↓
# src/repopilot/worker/bus.py:27
buffer = self._replay[run_id]
buffer.append(event)
if len(buffer) > self._replay_size:      # 只留最近 200 条
    del buffer[0]
for queue in self._subscribers[run_id]:
    queue.put_nowait(event)               # 广播给所有订阅者
```

**订阅侧**（HTTP 处理函数）：

```python
queue = bus.subscribe(run_id, replay=True)
   ↓
# src/repopilot/worker/bus.py:35
queue: asyncio.Queue = asyncio.Queue()
if replay:
    for event in self._replay[run_id]:
        queue.put_nowait(event)           # ← 先补历史
self._subscribers[run_id].append(queue)   # ← 再接实时
```

**回放缓冲是关键设计**：客户端先 `POST /runs`，再去连 SSE，中间可能已经过了几百毫秒，前两个节点早跑完了。没有回放，这些事件就永远看不到。有了回放，**晚到的订阅者也能看到完整过程**。

Java 对照：≈ Kafka 消费者从 earliest offset 开始读；或者 RxJava 的 `ReplaySubject`。

`defaultdict(list)` 的作用：访问一个不存在的键时**自动创建一个空列表**，不用先判断 `if run_id not in dict`。Java 对照：`computeIfAbsent`。

**结束时**：

```python
finally:
    if heartbeat_task is not None:
        heartbeat_task.cancel()          # 停心跳
    self.bus.close(row.id)               # 给所有订阅者塞 DONE 哨兵
    run_id_var.reset(token)              # 还原日志上下文
```

`finally` 保证这三件事**无论成功、失败、被取消都会做**。漏掉 `heartbeat_task.cancel()` 就是一个永远跑下去的僵尸协程。

**已知缺口（必须主动说）**：
> 事件总线是**进程内**的。API 和 worker 拆成两个进程后，SSE 就收不到 worker 那边的事件了。要拆得换 Redis pub/sub 或者 PG 的 `LISTEN/NOTIFY`。
> 但**业务正确性不受影响** —— 真相在数据库里，客户端轮询 `GET /runs/{id}` 拿到的结果完全一样。SSE 只是实时展示的优化。

这个"降级后依然正确"的论证，比单纯承认缺口有力得多。

---

# 站 17 · 人工审批

```bash
curl -X POST localhost:8000/runs/$RID/approval \
  -H 'content-type: application/json' \
  -d '{"decision":"approved","decided_by":"me","reason":"diff 看过了"}'
```

```python
# src/repopilot/api/routes.py:115
@router.post("/runs/{run_id}/approval", response_model=RunResponse)
async def decide_approval(run_id: UUID, body: ApprovalRequest) -> RunResponse:
    await _require(run_id)
    try:
        await approvals_repo.decide(run_id, decision=body.decision,
                                    decided_by=body.decided_by, reason=body.reason)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_row(await _require(run_id))
```

`ApprovalRequest.decision` 的类型是 `Literal["approved", "rejected"]` —— **传别的值 FastAPI 直接 422，压根进不来。**

```python
# src/repopilot/db/approvals.py:22
async def decide(run_id, *, decision, decided_by, reason=None) -> ApprovalRow:
    if decision not in ("approved", "rejected"):
        raise ValueError(...)

    target = RunStatus.PUBLISHING if decision == "approved" else RunStatus.REJECTED

    # 先流转状态（内含守卫 + 乐观锁）。非法流转会在这里抛出来，
    # 这样不会留下一条「审批了但状态没动」的孤儿记录。
    await transition(run_id, target, expected=RunStatus.PENDING_APPROVAL)

    async with transaction() as conn:
        record = await conn.fetchrow(
            "INSERT INTO approvals (run_id, decision, decided_by, reason) VALUES ($1,$2,$3,$4) RETURNING *",
            run_id, decision, decided_by, reason)
    return ApprovalRow.from_record(record)
```

## 三个设计点

**① 顺序：先流转，后记录**
如果反过来，重复审批时会先插入一条审批记录，然后流转失败抛异常 —— 留下一条**孤儿记录**（说批准了，但状态没动）。审计数据被污染了。

`expected=RunStatus.PENDING_APPROVAL` 是额外的一层：不光要求"能流转"，还要求"现在恰好是待审批"。

**② 审批是追加写，不是覆盖写**

```sql
CREATE TABLE approvals (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    decision text NOT NULL CHECK (decision IN ('approved','rejected')),
    decided_by text NOT NULL,
    reason text,
    decided_at timestamptz NOT NULL DEFAULT now()
);
```

`runs.status` 存**当前状态**（查询快），`approvals` 存**过程**（可追溯）。

**审计要的是"谁在什么时候基于什么理由做了什么决定"的历史**，只存当前值会丢掉"先驳回、改完再批准"这种真实过程。

`CHECK` 约束 = 数据库层的最后一道校验，代码写错了也插不进脏数据。

**③ 重复审批返回 409**

第二次调用时，状态已经是 `publishing` 了，`assert_transition(PUBLISHING, PUBLISHING)` 失败（自转非法）→ `InvalidTransition` → HTTP **409 Conflict**。

**这个行为是被测试覆盖的**（`tests/test_api.py`），不是碰巧。

**④ 审批时顺便交还租约**

```python
# src/repopilot/db/runs.py:163
RELEASE_LEASE = {"locked_by": None, "lease_expires_at": None}
```

`transition(..., **RELEASE_LEASE)` 把状态流转和"放开所有权"写进**同一条 UPDATE**。

分成两条语句的话，中间那一瞬间「状态已经是 `publishing`、租约还捏在前一个 worker 手上」，publisher 捞不到它，得干等一整个租约周期（120 秒）才接手。**一个只在两条语句之间存在的窗口，代价是 2 分钟的延迟。**

---

# 站 18 · 发布：`publishing → published`

审批只是把状态改成 `publishing`。真正开 PR 的是**第二条独立的循环**。

## 18.1 为什么不在审批的 HTTP 请求里直接发布

```python
# src/repopilot/worker/worker.py:95
async def _publish_loop(self) -> None:
    """和领取循环并排跑的第二个循环，专门处理审批通过的 run。

    为什么单独一个循环，而不是在审批的 HTTP 请求里直接发布？
      - 开 PR 要走网络，可能几秒到超时，HTTP 请求不该等它
      - 审批的人点完就该走，发布失败不能变成"批准失败"
      - 单独一个循环才能享受同一套租约机制：发布到一半崩了会被自动重试
    """
```

**这就是站 4 那个「`POST /runs` 只入队不执行」的同一个道理，只是换了个阶段。** 同一个原则在项目里出现两次，面试讲的时候可以点出来。

## 18.2 领取待发布的 run

```sql
-- src/repopilot/db/runs.py:112  CLAIM_PUBLISHING_SQL
UPDATE runs
   SET locked_by        = $1,
       lease_expires_at = now() + make_interval(secs => $2)
 WHERE id = (
     SELECT id FROM runs
      WHERE status = 'publishing'
        AND (lease_expires_at IS NULL OR lease_expires_at < now())
      ORDER BY created_at LIMIT 1
      FOR UPDATE SKIP LOCKED
 )
RETURNING *
```

和站 5 的 `CLAIM_SQL` 长得像，**三处关键差别**：

| 差别 | 为什么 |
|---|---|
| **不改 status** | `publishing` 已经是当前状态了，这里只是取得所有权。而且状态机里根本没有 `publishing → publishing` 的自转边，想改也改不了 |
| **不加 attempts** | `attempts` 是「Agent 执行」的预算。发布是另一码事，混在一个计数器里会互相污染 |
| 条件是「没人持有 **或** 租约已过期」 | 审批时已经把租约清空了（`RELEASE_LEASE`），所以刚批准的 run 立刻能被捞到 |

## 18.3 发布本身：只依赖数据库里的 diff

```python
# src/repopilot/publishing/github.py:80
async def publish(self, row: RunRow) -> PublishResult:
    ref = parse_external_ref(row.external_ref)          # "owner/repo#42" → ("owner/repo", 42)
    if ref is None:
        raise PublishError(...)
    repo, issue_number = ref
    if not (row.diff or "").strip():
        raise PublishError("这个 run 没有 diff，没有东西可发布")

    branch = branch_name(row.id)                         # repopilot/run-<id前8位>
    workdir = Path(tempfile.mkdtemp(prefix=f"publish-{str(row.id)[:8]}-"))
    try:
        await self._prepare_branch(workdir, row, branch)   # clone → 建分支 → apply → commit
        await self._push(workdir, repo, branch)
        return await self._open_pr_and_comment(row, repo, issue_number, branch)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
```

⭐ **最重要的设计决定：发布不依赖磁盘上的 workspace，只依赖数据库里的 `runs.diff`。**

> Agent 跑完到人批准之间可能隔几小时。中间进程重启过、`.workspaces/` 被清理过都很正常。
> 所以发布是**重新 clone 一份，把 `runs.diff` 打上去** —— 整个过程可以在任何一台机器上重放。

**面试口径**：**"审批闸门意味着执行和发布之间有一段任意长的人类时间。所以发布必须只依赖持久化的状态，不能依赖执行时留下的临时目录。"**

## 18.4 幂等：分支名就是幂等键

```python
def branch_name(run_id) -> str:
    return f"repopilot/run-{str(run_id)[:8]}"
```

**确定性的分支名 = 天然的幂等键。**

- push 同样的提交 → git 层面是 **no-op**
- 开 PR 之前先查「这个 head 分支上有没有开着的 PR」：

```python
existing = await self.client.find_pull_request(repo, head_branch=branch)
if existing is not None:
    log.info("run=%s 分支 %s 上已有 PR，复用不重开", row.id, branch)
    pr = existing
else:
    base = await self.client.get_default_branch(repo)
    pr = await self.client.create_pull_request(repo, head=branch, base=base, ...)
```

**所以 publisher 在任何一步崩掉，重试都不会开出第二个 PR。** 「push 成功、开 PR 之前崩了」这个最难缠的中间态，靠这一次查询捡回来。

对比一下站 17 那个 webhook 幂等：那边用**数据库唯一约束**，这边用**远端资源的确定性命名 + 查重**。**幂等的实现方式取决于谁是权威**：本地数据库是权威时用唯一约束，远端服务是权威时用确定性 ID + 查询。

## 18.5 失败分两类（和站 5 是同一个哲学）

```python
# src/repopilot/worker/worker.py:126
except PublishError as exc:
    # 逻辑失败：重试也是一样的结果，直接进终态，等人来看。
    await runs_repo.transition(row.id, RunStatus.FAILED,
                               error=f"发布失败: {exc}", finished_at=datetime.now(UTC),
                               **runs_repo.RELEASE_LEASE)
    return True

# 注意这里没有 except Exception：网络抖动之类的异常故意让它冒到
# _publish_loop 去。租约不续 → 过期 → 下一轮被重新领取。
await runs_repo.transition(row.id, RunStatus.PUBLISHED,
                           branch=result.branch, pr_url=result.pr_url,
                           finished_at=datetime.now(UTC), **runs_repo.RELEASE_LEASE)
```

具体的分界线在 HTTP 状态码上：

```python
# src/repopilot/publishing/github.py:181
except GitHubError as exc:
    # 4xx 是我们自己的问题（权限/参数），重试没意义 → 不重试。
    # 5xx / 429 是对方的问题，值得重试。
    if 400 <= exc.status_code < 500 and exc.status_code != 429:
        raise PublishError(str(exc)) from exc
    raise
```

**429（限流）被特意从 4xx 里挑出来归到"该重试"** —— 它虽然是 4xx，但语义是"待会再来"。这个细节值得讲。

**Java 对照**：`PublishError` = 进死信队列；其他异常 = nack 重新投递。

## 18.6 三个"踩过才知道"的细节

**① `GIT_TERMINAL_PROMPT=0`**

```python
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",          # 兜底：真有人问密码，就回一个空串
    "GIT_CONFIG_NOSYSTEM": "1",     # 别读机器上的全局 git 配置，行为要可复现
}
```

> token 过期时 git 默认会弹出**交互式的用户名/密码提示**。在一个没有 tty 的后台进程里，它会一直挂着，直到墙钟超时才被杀掉。设成 0 让它立刻失败，错误信息也清楚得多。

**② 报错信息里要脱敏**

```python
await self._git([..., "git", "push", self._remote_url(repo), ...],
                redact=self._token_values())     # remote URL 里有 PAT！
```

remote URL 是 `https://x-access-token:<PAT>@github.com/owner/repo.git`。git 报错时会把 URL 原样打出来 —— **不脱敏就等于把 token 写进了日志和数据库的 error 字段**。

```python
for secret in redact or []:
    detail = detail.replace(secret, "***")
```

**③ 补丁文件放在 clone 外面**

```python
patch = workdir / "run.patch"          # workdir/run.patch，不是 clone/run.patch
```

> 放里面的话，下面 `git add -A` 会把这个补丁文件**一起提交进 PR**。

还有 `strip_diff_preamble`：`git_diff` 工具的输出带 `--stat` 摘要，`git apply` 大多数时候能自己跳过前言，**但那是"大多数时候"** —— 显式从第一行 `diff --git ` 开始截，不赌。

## 18.7 无 token 时降级

```python
# src/repopilot/publishing/__init__.py
def build_publisher(settings: Settings) -> Publisher:
    if not settings.github_token:
        return DryRunPublisher()
    return GitHubPublisher(settings, GitHubClient(settings.github_token, ...))
```

和 `build_llm()` 一模一样的模式：**协议 + 真实现 + 假实现**，worker 只依赖协议。

注意这里有个看似矛盾、其实标准不同的设计：

> **验签没配密钥 → fail closed，全部拒绝。**
> **发布没配 token → 降级空转，服务照常启动。**
>
> 因为**验签是安全边界，缺配置必须拒绝；发布是功能，缺配置降级并且说清楚**（`pr_url` 留空就代表没真发出去）。

**这个对比是很好的面试材料** —— 说明你不是机械套用"安全默认"，而是分场景判断。

---

# 站 19 · 五个「如果这里崩了会怎样」

面试官最爱问这个。答案全在上面，这里汇总成表。

| 崩溃点 | 现象 | 恢复机制 | 会丢任务吗 |
|---|---|---|---|
| **HTTP 返回 202 之前** | 客户端拿到 5xx | 事务回滚，什么都没发生 | 不会（还没承诺） |
| **返回 202 之后、worker 领取之前** | run 停在 `queued` | worker 起来自然会领 | **不会** |
| **Agent 跑到一半，worker 被 `kill -9`** | run 卡在 `running`，没人续租 | 租约 120 秒后过期 → `CLAIM_SQL` 的第二个分支把它捞回来 → `attempts+1` 重跑 | **不会** |
| **优雅停机时还在跑** | `stop()` → 等 30 秒 → 超时的 `task.cancel()` | `CancelledError` 分支**故意不改状态** → 同上，靠租约回收 | **不会** |
| **反复失败，`attempts` 用尽** | `attempts >= max_attempts` | reaper 每 120 秒扫一次，标记 `failed` + 写错误原因 | 不会无限打转 |
| **push 成功但开 PR 之前崩了** | run 停在 `publishing`，远端已有分支 | 租约过期 → 重新领取 → **先查 head 分支上有没有 PR，有就复用** | 不会开出两个 PR |
| **发布时 GitHub 返回 4xx** | 权限/参数问题 | 抛 `PublishError` → 直接标 `failed`，**不重试** | 进终态等人看 |
| **发布时 GitHub 返回 5xx / 429** | 对方的问题 | 异常冒到发布循环 → 租约过期 → 下一轮重试 | 不会 |
| **数据库挂了** | 所有操作抛异常 | 没有兜底，`/health` 会显示 `degraded` | **会**（这是已知缺口） |

reaper：

```python
# src/repopilot/db/runs.py:117
async def reap_exhausted() -> int:
    sql = """
        UPDATE runs
           SET status = 'failed',
               error = COALESCE(error, 'worker 失联且重试次数已用尽'),
               finished_at = now()
         WHERE status = 'running'
           AND lease_expires_at < now()
           AND attempts >= max_attempts
    """
    result = await get_pool().execute(sql)
    count = int(result.rsplit(" ", 1)[-1])
```

`COALESCE(error, '...')` —— **已经有错误信息就别覆盖**，保留最原始的失败原因。

**Java 对照**：整套机制 = RabbitMQ 的 **ack 超时重投 + 死信队列**，或者 Redisson 的看门狗。区别是这里的"锁"只是数据库里一个时间戳字段，没有引入任何中间件。

---

# 站 20 · 一次完整的日志长什么样

跑 `make run` 然后发一个请求，你会看到（简化过）：

```
... INFO [run=-]        repopilot.db.pool     | pg pool ready (min=2 max=10)
... INFO [run=-]        repopilot.worker      | worker MacBook-12345 启动 (并发上限=2, 租约=120s)
... INFO [run=-]        repopilot.api.app     | RepoPilot 就绪 | provider=scripted model=... db=localhost:5433/repopilot
... INFO [run=-]        repopilot.db.runs     | worker=MacBook-12345 领取 run=3f2b1c8a-... (第 1 次尝试)
... INFO [run=3f2b1c8a] repopilot.workspace   | workspace created at .workspaces/3f2b... from fixtures/sample_repo
... INFO [run=3f2b1c8a] repopilot.tools.base  | tool=list_files ok=True 2ms
... INFO [run=3f2b1c8a] repopilot.tools.base  | tool=read_file ok=True 1ms
... INFO [run=3f2b1c8a] repopilot.tools.base  | tool=search_code ok=True 3ms
... INFO [run=3f2b1c8a] repopilot.tools.base  | tool=write_file ok=True 1ms
... INFO [run=3f2b1c8a] repopilot.sandbox     | cmd /path/python -m pytest exit=0 timed_out=False 412ms
... INFO [run=3f2b1c8a] repopilot.tools.base  | tool=run_tests ok=True 415ms
... INFO [run=3f2b1c8a] repopilot.tools.base  | tool=git_diff ok=True 22ms
... INFO [run=3f2b1c8a] repopilot.db.runs     | run=3f2b1c8a-... running → pending_approval
```

**注意 `[run=...]` 这一列**：领取那条还是 `-`（因为 `run_id_var` 还没设，那行日志是在仓储层打的），从 workspace 创建开始全部带上了 run_id。**这就是 ContextVar 的效果**（站 7）。

多个 run 并发时，这一列让你能把交错的日志按 run 分开看 —— 这是 asyncio 项目里排查问题的生命线。

---

# 站 21 · 另一个入口：GitHub Webhook

主线是手动 `POST /runs`。还有第二个入口 —— GitHub 上给 Issue 打个 `repopilot` 标签，自动入队。

**两条入口在站 3（`create_run`）汇合**，往后的链路一行都不用改。这就是分层的价值。

## 20.1 顺序是有讲究的，每一步都不能挪

```python
# src/repopilot/api/routes.py:140
@router.post("/webhooks/github", response_model=WebhookResponse)
async def github_webhook(request: Request, response: Response,
                         settings: Settings = Depends(get_settings)) -> WebhookResponse:
    body = await request.body()                    # 1. 读原始字节

    if not verify_signature(settings.github_webhook_secret, body,
                            request.headers.get(SIGNATURE_HEADER)):
        raise HTTPException(status_code=401, detail="签名校验失败")     # 2. 验签

    delivery_id = request.headers.get(DELIVERY_HEADER)
    if not delivery_id:
        raise HTTPException(status_code=400, detail=f"缺少 {DELIVERY_HEADER}")
    event_type = request.headers.get(EVENT_HEADER, "")
    payload = json.loads(body or b"{}")            # 解析
    if event_type == "ping":
        return WebhookResponse(status="pong", detail="webhook 已连通")

    is_new = await deliveries_repo.claim_delivery(delivery_id, source="github",
                                                  event_type=event_type, payload=payload)
    if not is_new:
        return WebhookResponse(status="duplicate", ...)                # 3. 幂等

    if event_type != "issues":
        return WebhookResponse(status="ignored", ...)
    trigger = extract_issue_trigger(payload, trigger_label=settings.github_trigger_label)
    if trigger is None:
        return WebhookResponse(status="ignored", ...)                  # 4. 授权判断

    row = await runs_repo.create_run(task=trigger.to_task(), ...,      # 5. 入队
                                     source="github_issue",
                                     external_ref=trigger.external_ref)
    await deliveries_repo.attach_run(delivery_id, row.id)
    response.status_code = 202
    return WebhookResponse(status="queued", run_id=row.id, external_ref=trigger.external_ref)
```

| 步骤 | 为什么不能挪 |
|---|---|
| **① 读原始字节** | 验签必须对原文做。`json.loads` 再 `json.dumps` **不是恒等操作**（key 顺序、空格都会变），签名一定对不上。所以参数写的是 `request: Request` + `await request.body()`，不是 `body: dict` |
| **② 先验签再记账** | 反过来的话，**任何人都能往你的幂等台账里灌垃圾** —— 而且因为 delivery_id 已经被占，真正的投递到达时会被判成重复直接丢掉。这是一个"看起来只是顺序"的安全漏洞 |
| **③ 幂等在业务判断之前** | GitHub 10 秒内收不到 2xx 会重投最多 3 次。不去重的话一个 Issue 会开三个 PR |
| **④ 默认不响应** | 仓库里**所有** Issue 都会推事件过来。只有人主动打上 `repopilot` 标签才算授权 |
| **⑤ 所有分支都返回 2xx** | 返回 4xx/5xx 会触发 GitHub 重投，而"这个 Issue 没打标签"根本不是需要重投的错误 |

## 20.2 验签的两个安全要点

```python
# src/repopilot/github/webhook.py:64
if not secret or not header:
    return False
expected = sign(secret, body)
return hmac.compare_digest(expected.encode(), header.encode())
```

**① fail closed** —— **密钥没配置就拒绝所有请求**，而不是"没配就跳过验签"。

> 默认放行的开关是最典型的生产事故：某次部署漏注入一个环境变量，接口就裸奔了，而且日志里一条报错都没有。

**② 常数时间比较** —— 绝对不能写成 `expected == header`。

> `==` 一发现某个字节不同就立刻返回，**耗时因此泄露了"前面几位猜对了"这个信息**。攻击者反复请求、测量响应时间，就能一个字节一个字节把签名试出来（timing attack）。`hmac.compare_digest` 无论第几位不同都走完全程。
>
> **Java 对照：`MessageDigest.isEqual`。做支付回调验签、比对 API token 用的是同一个东西，绝不用 `String.equals`。**

还有个细节：`.encode()` 不是多余的。`compare_digest` 传 `str` 时**要求纯 ASCII**，否则抛 `TypeError` —— 而 header 是攻击者完全可控的，塞个中文进来就能把 401 变成 500。转成 bytes 就没这个问题。

## 20.3 这个模块为什么这么好测

```python
"""这个模块刻意**不碰数据库、不碰 FastAPI**。输入是 `bytes` 和 `dict`，
输出是布尔值和一个小 DTO。所以它能被当纯函数测试 ——
不用起服务、不用连库、不用造 Request 对象。安全相关的代码越容易测越好。"""
```

`verify_signature` 和 `extract_issue_trigger` 都是**纯函数**。所以 `tests/test_webhook.py` 可以穷举各种攻击输入（空密钥、篡改 body、错前缀、非 ASCII header），一个测试跑几毫秒。

**面试口径**：**"安全相关的代码要设计成纯函数，因为它必须被穷举测试。"**

## 20.4 已知取舍（主动说）

> 第 3 步之后如果失败，会有一个缺口：投递已登记但 run 没建成，重投也会被判重，**事件就丢了**。
>
> 工业级做法是把"登记 + 入队"放进**同一个事务**（两张表在同一个库里，做得到 —— `claim_delivery` 和 `create_run` 都预留了 `conn` 参数）。这里没做，是刻意留的取舍。

---

# 一分钟版（面试开场用这个）

> 客户端 `POST /runs`，接口**只做入队**：写一行到 `runs` 表就返回 202。这张表同时是业务实体和任务队列。
>
> worker 在后台循环，**先拿并发令牌再去领任务**（没能力处理就不占位），领取用的是一条 `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING *` —— 选中和标记在同一条语句里，多个 worker 互不阻塞。领取时写入**租约到期时间**，执行期间后台协程每 40 秒续租一次。
>
> 拿到任务后，**把目标仓库 copytree 一份**，打一个 git 基线提交，然后跑 LangGraph 的六节点图：analyze → plan → execute → run_tests → evaluate，evaluate 通过条件边决定是重试还是收尾。每个工具调用都被注册表包了限流、超时和异常转换，模型给的每个路径都过 `Workspace.resolve()` 做收敛，跑测试走无 shell 的子进程、超时杀整个进程组。
>
> 图跑完，**用测试结果 + 是否真的改了文件**来判定成败，不听 Agent 自称。成功也只能到 `pending_approval` —— 状态机里 `running` 到 `published` 根本没有直达的边，**这一条约束就是审批闸门的全部实现**。
>
> 人工批准后进 `publishing`，交给**第二条并排的发布循环**：重新 clone 目标仓库、把数据库里存的 diff 打上去、push 一个**确定性命名**的分支、开 PR、回写 Issue 评论。发布只依赖数据库里的 diff，不依赖执行时的临时目录 —— 因为审批闸门意味着中间隔着任意长的人类时间。分支名就是幂等键，开 PR 前先查重，所以任何一步崩掉重试都不会开出第二个 PR。
>
> 全程 worker 崩了不丢任务：租约过期后任务被别的 worker 捞回来重试；重试次数用尽的由 reaper 标记失败。失败严格分两类 —— 逻辑失败进终态等人看，基础设施失败靠租约自动重试。

---

下一章：[06-faq.md](06-faq.md) —— 排错手册 + 面试问答。
