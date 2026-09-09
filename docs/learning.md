# Interview-worthy notes

Only things worth saying out loud in an interview. Add as you go.

---

## `asyncio.Semaphore`

**What.** A permit counter. `async with sem:` takes a permit, blocks when none are left,
releases on exit.

**Why here.** `plan` fires every `read_file` and `search_code` concurrently with
`asyncio.gather`. Without a cap, an LLM that names 40 files opens 40 file handles /
subprocesses at once. The semaphore lives in `ToolRegistry`, so *every* tool inherits the
cap and no individual tool has to think about it.

**Java.** `java.util.concurrent.Semaphore`, same idea. Difference: this one blocks a
coroutine, not an OS thread, so a waiting task costs ~nothing.

**One-liner.** "Concurrency is capped in the tool registry, not per call site, so adding
a tool can't accidentally widen the blast radius."

---

## `asyncio.gather` vs threads

**What.** `gather` schedules N coroutines on one event loop thread and waits for all.

**Why here.** Tool work is I/O-bound: file reads, subprocesses, LLM HTTP calls. The loop
switches at every `await`, so N reads overlap on one thread.

**Java.** Closest is `CompletableFuture.allOf`. But Java's default is a thread pool;
asyncio is one thread with cooperative switching — cheaper, and no locking needed for
plain state mutation between awaits.

**One-liner.** "Agent tools are I/O-bound, so concurrency beats parallelism; one event
loop gets the throughput without thread-pool overhead."

**Trap to mention.** A blocking call (`requests.get`, `time.sleep`, big CPU loop) inside
a coroutine freezes the *whole* loop, including unrelated runs. That is why
`sandbox/local.py` uses `asyncio.create_subprocess_exec`, not `subprocess.run`.

---

## `asyncio.wait_for` and timeouts

**What.** Wraps an awaitable; on expiry it **cancels** the inner task and raises
`TimeoutError`.

**Why here.** `ToolRegistry.call` wraps every tool. A hung tool can't stall the graph.

**Java.** `Future.get(timeout)` — but that only stops *waiting*, the work keeps running.
`wait_for` actually cancels the coroutine. Closer to `future.cancel(true)`.

**Trap.** Cancelling a coroutine does **not** kill a child process it spawned. That is
why `run_command` sets `start_new_session=True` and calls `os.killpg` on timeout —
otherwise pytest's children survive as orphans holding the workspace open.

---

## `ContextVar`

**What.** Per-task storage. Each asyncio Task inherits a copy of the current context.

**Why here.** `run_id_var` is set once in `RunService._execute` and every log line from
every node and tool picks it up, with no plumbing through function signatures.

**Java.** `ThreadLocal`, or `MDC` in SLF4J — this is exactly the MDC pattern. Difference:
`ThreadLocal` breaks across a thread pool hand-off; `ContextVar` is copied per Task, so
it survives `create_task` and `gather`.

---

## LangGraph state and reducers

**What.** State is a `TypedDict`. A node returns a **partial** dict; LangGraph merges it.
A key annotated `Annotated[list[X], operator.add]` gets *combined* instead of overwritten.

**Why here.** `tool_calls`, `errors` and `step_log` accumulate across three retry passes
through `execute`. Without reducers each node would have to read the old list, copy it,
append, and write it back — and two concurrent nodes would clobber each other.

**Java.** No direct analogue. Closest mental model: a reducer is `Collectors.reducing`
applied per-key on every state merge, or an event-sourced fold.

**One-liner.** "Nodes return diffs, not the whole state; the reducer decides merge
semantics per key."

---

## Why a graph instead of a `while` loop

The retry path, the budget check and the exit condition are **declared as edges**, so
they are data: inspectable, streamable, and testable in isolation
(`test_conditional_edge_routes_on_verdict` calls the router as a plain function).
A `while` loop hides the same logic inside control flow you can only test by running the
whole thing. It also gives streaming and checkpointing for free.

**Honest caveat.** For a 6-node linear-plus-one-retry flow, a `while` loop would work.
The payoff arrives when you add branches (human approval, multiple executors).

---

## Structured output via forced tool use

**What.** Instead of asking for JSON in prose and parsing it, declare a tool whose
`input_schema` is your Pydantic model's JSON schema and set
`tool_choice={"type": "tool", "name": ...}`. The model must emit a conforming object.

**Why here.** Nodes never string-parse. `Analysis`, `Plan` and `EditSet` arrive as
validated Pydantic instances, and a schema violation is a typed `LLMError` at the
boundary rather than an `AttributeError` three nodes later.

**Java.** Same role as Spring AI's `BeanOutputConverter`, but validated server-side.

---

## Pydantic v2 `BaseModel` vs `TypedDict`

- `BaseModel` — runtime **validation** and coercion. Cost: object construction.
  Used at trust boundaries: HTTP bodies, LLM output, tool results.
- `TypedDict` — a **type-checker-only** annotation on a plain `dict`. Zero runtime cost,
  zero validation. Used for `AgentState`, because LangGraph merges it as a dict.

**Java.** `BaseModel` ≈ a DTO with Bean Validation annotations actually enforced.
`TypedDict` ≈ a `Map<String,Object>` that only the compiler pretends is typed.

**One-liner.** "Validate at the boundary, stay cheap inside."

---

## 用 Postgres 表当任务队列

**核心 SQL**（`db/runs.py::CLAIM_SQL`）：外层 `UPDATE` 包一个内层
`SELECT ... ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`，最后 `RETURNING *`。

**`SKIP LOCKED` 解决什么**：

| 写法 | 10 个 worker 同时抢的结果 |
|---|---|
| 什么都不加 | 全读到同一行，**同一个任务执行 10 次** |
| `FOR UPDATE` | 排队等锁，退化成串行，**吞吐崩了** |
| `FOR UPDATE SKIP LOCKED` | 跳过已锁住的行，各拿各的，互不阻塞 |

**别说错**：`SKIP LOCKED` **MySQL 8.0 也有**，不是 PG 独有。PG 在这里的真正优势是
`RETURNING`（一条语句完成「领取 + 拿到内容」，MySQL 得再查一次且中间有并发窗口）
和**部分索引**（`CREATE INDEX ... WHERE status IN (...)`，队列表 99% 是历史数据，
全量索引又大又没用；MySQL 没这功能）。

**什么时候该用 MQ 而不是数据库表**：吞吐到万级 TPS、需要扇出/多消费组、
需要跨服务解耦时。我们这里任务是分钟级的、量小，且**任务本身就是业务实体
（要查询、要审批、要展示历史）**，用表更省事，还免去「MQ 和 DB 双写不一致」。
这个取舍要能主动讲。

**一句话**：「队列和业务表是同一张表，所以入队和业务写入天然在一个事务里，
不存在双写不一致。」

---

## 租约（lease）：不依赖进程活着的可靠性

worker 不是「拿一把锁直到干完」，而是**租一段时间，到期不续租就自动失效**。

- 领取时写 `locked_by` + `lease_expires_at = now() + N 秒`
- 干活期间后台协程定期 `heartbeat` 续租
- worker 被 `kill -9` → 没人续租 → 租约过期 → 任务被别的 worker 领走
- `attempts >= max_attempts` 的僵尸由 reaper 标记 failed，防止无限打转

**Java 对照**：等价于 RabbitMQ 的 **ack 超时重投**，或 Redisson 看门狗续期。
区别是这里的「锁」只是数据库里一个时间戳字段，没有额外中间件。

**续租失败必须让 worker 知道**：`heartbeat` 的 `WHERE locked_by = $2` 条件，
租约已被别人抢走就返回 False —— 否则会出现两个 worker 同时写一个 run。

---

## 幂等：为什么必须靠数据库唯一约束

```sql
INSERT INTO webhook_deliveries (...) VALUES (...)
ON CONFLICT (delivery_id) DO NOTHING
RETURNING delivery_id
```

`DO NOTHING` 时**零行返回**，所以 `record is None` 就等于「重复投递」。
**判断和登记是同一条语句**，天然原子。

**先 SELECT 再 INSERT 为什么是错的**：N 个并发请求会同时读到「不存在」，
然后全都以为自己是第一个。`test_concurrent_duplicates_only_one_wins` 用 20 个
并发协程专门打这个错。**唯一约束是唯一可靠的仲裁者。**

**MySQL 对照**：`INSERT IGNORE` / `ON DUPLICATE KEY UPDATE` 效果类似，
但**没有 `RETURNING`**，必须再查一次才知道是不是自己插进去的。

**一句话**：「幂等键的判重不能放在应用层，要放在数据库的唯一约束上。」

---

## 状态机为什么要表驱动

把「谁能变成谁」写成一张 `dict[状态, frozenset[状态]]`，而不是散落各处的 if。

- 非法流转在**一个地方**被挡住
- 这张表本身可以被测试（用 BFS 验证「每个活跃状态都能到达终态」，防死角）
- 终态 = 没有出边的状态，**从表推导**，不手写第二份清单

**两层保护缺一不可**：
1. 应用层 `assert_transition` —— 挡业务上非法的流转（`running` 不能直接 `published`）
2. 数据库层 `UPDATE ... WHERE status = 当前状态` —— 乐观锁，挡「读到写之间被人改了」

**为什么自转也非法**：第 2 层用 `status` 本身当版本号，允许原地踏步会让乐观锁失效。

**Java 对照**：第 2 层就是 JPA 的 `@Version`，只是这里用 status 当版本号。

**一句话**：「Agent 说成功不算成功 —— `running` 到 `published` 没有直达的边，
必须经过 `pending_approval`。这一条约束就是审批闸门的全部实现。」

---

## Webhook 验签：三个必须说对的点

**1. 必须对 raw bytes 验，不能对反序列化后的对象验。**
JSON 反序列化再序列化不是恒等操作 —— key 顺序、空格、Unicode 转义都会变，
签名必然对不上。所以路由签名是 `request: Request` + `await request.body()`，
而不是 `body: dict`。**Java 对照**：Spring 里同一个坑，得用
`ContentCachingRequestWrapper` 或 `@RequestBody String` 才能拿到原文。

**2. 比较必须常数时间。**
`==` 一发现某字节不同就返回，耗时泄露了「前面几位猜对了」。攻击者测量响应
时间可以逐字节把签名试出来（timing attack）。用 `hmac.compare_digest`。
**Java 对照**：`MessageDigest.isEqual(byte[], byte[])`，做支付回调验签用的
就是它，绝不用 `String.equals`。

**3. 密钥没配置要 fail closed。**
`if not secret: return False`，而不是「没配就跳过验签」。默认放行的开关是最
典型的生产事故：某次部署漏注入一个环境变量，接口就裸奔了且日志无异常。

**加分细节**：`compare_digest` 传 `str` 时要求纯 ASCII，否则抛 `TypeError`。
签名头是攻击者完全可控的，塞个中文进来就把 401 变成 500。所以比较前
`.encode()` 成 bytes。「攻击者可控的输入不能有让服务端抛异常的路径」。

---

## 幂等和验签的先后顺序

正确顺序是 **验签 → 记账 → 处理**，反过来是漏洞：先记账再验签，等于任何人
不需要知道密钥就能往你的幂等台账里灌垃圾 delivery_id，之后 GitHub 真正的
投递撞上这些 id 会被判成重复，事件直接被吞掉。

**「忽略」要返回 2xx，不是 4xx。** 没打触发标签的 Issue 不是错误。返 4xx 会让
GitHub 反复重投一个我们根本不想处理的事件，还会把 webhook 标成失败。
只有「签名不对」和「请求本身畸形」才配 4xx。

**已知缺口（要主动说）**：「登记投递」和「入队 run」不在同一个事务里。两者之间
崩溃 → 投递已记账、run 没建成、重投被判重，事件丢了。两张表在同一个库，
技术上一个事务就能解决，是刻意留的取舍。

---

## Why a coding agent needs a sandbox

Three distinct risks, three distinct mitigations:

1. **Wrong path** — model writes `../../.ssh/authorized_keys`. → `Workspace.resolve()`.
2. **Runaway process** — generated test loops forever. → wall-clock timeout + `killpg`.
3. **Blast radius** — a bad edit corrupts the real repo. → operate on a `copytree`.

"Sandbox" is not one feature; naming the three separately is the good answer.
Also worth saying: there is **no general shell tool**, because a shell tool makes every
other restriction decorative.
