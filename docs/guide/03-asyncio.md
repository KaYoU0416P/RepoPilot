# 03 · asyncio 深入浅出

本项目 60% 的函数带 `async`。这一章把它讲透，讲到你能回答"await 的那一刻到底发生了什么"。

---

## §1 先建立正确的画面：一个服务员，十张桌子

**多线程的画面**（Java 的默认思路）：
> 十张桌子，雇十个服务员，一人盯一桌。客人看菜单的时候服务员就在旁边**站着干等**。
> 人多了雇不起（线程栈内存），而且十个服务员要抢同一个后厨（加锁）。

**asyncio 的画面**：
> 十张桌子，**一个**服务员。
> 走到 1 桌 → "点什么？" → 客人说"我看看" → 服务员**不等**，立刻走到 2 桌 → ……
> 1 桌喊"好了" → 服务员回去接着服务 1 桌。

关键：**服务员从不站着干等**。只要有人在"思考"（= 等 I/O），他就去干别的活。

对应到代码：

- **服务员** = 事件循环（event loop）
- **"我看看菜单"这段等待** = `await`
- **每张桌子** = 一个协程 / Task

⚠️ **最重要的推论**：只要服务员**自己**开始洗盘子（CPU 密集计算）或者站着发呆（`time.sleep`），**十张桌子全部卡死**。这就是"阻塞调用毒死事件循环"，见 §6。

---

## §2 `async def` 和 `await` 的真实语义

### 2.1 `async def` 定义的函数，调用它不会执行

```python
async def f():
    print("跑了")

f()          # ← 什么都不打印！只是造了一个"协程对象"
await f()    # ← 这才真的跑
```

**这是新手第一个大坑。** `async def` 的函数调用后返回的是一个**待办事项对象**，不是结果。必须 `await` 它，或者交给事件循环去跑，才会真的执行。

比喻：`f()` 是**写了一张工单**，`await f()` 是**把工单交给服务员并等他做完**。

### 2.2 `await` 的两个含义

`await x` 同时做两件事：

1. **"我要 x 的结果，没有就先别往下走"**（像 Java 的 `future.get()`）
2. **"在等的这段时间，事件循环你去跑别的"**（Java 的 `get()` 做不到，它会占着线程干等）

第 2 点是全部价值所在。

### 2.3 `await` 只能出现在 `async def` 里面

```python
def f():
    await g()        # ❌ SyntaxError
```

这导致 **async 会传染**：一个函数里想 `await`，它自己就得是 `async`；调它的人也得 `async`……一路传到顶。

所以本项目从 `routes.py` 的处理函数一路 async 到 `sandbox/local.py`。顶层由谁来启动？由 **uvicorn**（它跑着事件循环）。`scripts/demo.py` 里则是 `asyncio.run(main())` 手动起一个。

Java 对照：这个"传染性"和 Reactor（`Mono`/`Flux`）一模一样 —— 一旦响应式，整条链路都得响应式。

### 2.4 协程之间的切换点是明确的

**协程只会在 `await` 处让出控制权。** 两个 `await` 之间的代码是原子的，不会被打断。

所以下面这段**完全线程安全**，不需要锁：

```python
# src/repopilot/worker/runner.py:43
def emit(event_type, node=None, message="", **data):
    nonlocal seq
    seq += 1                     # 没有 await，不可能被切走
    self.bus.publish(...)
```

Java 里 `seq++` 在多线程下必须加锁或用 `AtomicInteger`。这是 asyncio 的一个实实在在的心智负担减免。

**面试口径**：
> "asyncio 是协作式调度，切换点只在 `await`，所以共享状态的读改写只要不跨 `await` 就是原子的，绝大多数场景不需要锁。代价是任何一个阻塞调用都会卡死整个循环 —— 心智负担从'到处加锁'变成了'到处别阻塞'。"

---

## §3 Task：让协程"在后台跑起来"

`await f()` 是**等它做完**。如果想让它**在后台跑，自己继续往下**，用 `create_task`：

```python
# src/repopilot/api/app.py:41
app.state.worker_task = asyncio.create_task(worker.run_forever(), name="worker")
```

`create_task` 立刻返回一个 `Task` 对象，协程被丢进事件循环的待办队列，下次有空档就跑。

Java 对照：`executor.submit()` 返回 `Future`。

本项目三处用到：

```python
reaper = asyncio.create_task(self._reaper_loop(), name="reaper")           # worker.py:48
task = asyncio.create_task(self._guarded_execute(row), name=f"run-{row.id}")  # worker.py:64
heartbeat_task = asyncio.create_task(self._heartbeat_loop(...), name=f"hb-{row.id}")  # runner.py:64
```

三个都是**永远不打算 await 的后台循环**（心跳、清理、执行）。

### ⚠️ Task 会被垃圾回收！

事件循环只持有 Task 的**弱引用**。如果你不把它存起来，它可能在跑到一半时被 GC 掉。所以本项目：

```python
# src/repopilot/worker/worker.py:38, 65-66
self._inflight: set[asyncio.Task] = set()
...
self._inflight.add(task)
task.add_done_callback(self._inflight.discard)
```

- `add` 进集合 = 拿住强引用，不让 GC 收
- `add_done_callback(self._inflight.discard)` = 干完了自动从集合里移除，不泄漏

`discard` 而不是 `remove`：`discard` 在元素不存在时不报错。

这两行不是可有可无的样板代码，**漏了就是随机的任务消失**。这也是一个好面试素材。

### `name=` 参数

给 Task 起名字，debug 时 `asyncio.all_tasks()` 能看清是谁。生产环境排查卡死的时候救命。

---

## §4 并发工具箱（本项目全部用到了）

### 4.1 `asyncio.gather` —— 一起跑，全部等

```python
# src/repopilot/agent/nodes.py:70-77
read_jobs = [self.registry.call("read_file", self.ws, path=p) for p in analysis.relevant_files[:5]]
search_jobs = [self.registry.call("search_code", self.ws, pattern=q) for q in analysis.search_queries[:3]]
results = await asyncio.gather(*read_jobs, *search_jobs)
```

拆开看：

1. `self.registry.call(...)` 是 `async def`，调用它**不执行**，只生成协程对象（见 §2.1）。
2. 所以 `read_jobs` 是一个装着 5 个"待办工单"的列表，此刻**一个文件都还没读**。
3. `*read_jobs, *search_jobs` 把两个列表摊平成 8 个参数。
4. `await asyncio.gather(...)` 把 8 个工单一起交给事件循环，**并发**执行，全部完成后返回结果列表。
5. **结果顺序 = 传入顺序**，不是完成顺序。所以后面能安全地 `zip` 配对。

耗时从 "8 次串行 I/O" 变成 "最慢那一次"。

Java 对照：`CompletableFuture.allOf(...).join()`，但那边默认跑在 ForkJoinPool 的线程上，这边全在一个线程。

**如果其中一个抛异常**：`gather` 默认会立刻把异常抛给调用方。本项目不担心 —— 因为 `registry.call` 内部已经把所有异常吸收成 `ToolResult(ok=False)` 了（见 [02-syntax.md](02-syntax.md) §13）。**这是分层设计的回报：上层不用写 try。**

### 4.2 `asyncio.Semaphore` —— 限流令牌

```python
# src/repopilot/tools/base.py:56
self._semaphore = asyncio.Semaphore(max_concurrency)     # 默认 4

# 使用：
async with self._semaphore:
    result = await asyncio.wait_for(spec.fn(workspace, **kwargs), timeout=...)
```

比喻：**四把钥匙挂在墙上**。要干活先拿一把，没有就在门口排队；干完把钥匙挂回去。

`async with` 保证"挂回去"这一步在任何情况下都会发生（包括抛异常）。

**为什么放在注册表而不是每个工具里**：因为这样**新加一个工具自动就受限**，不用作者记得加。上一节 `gather` 一次性发 8 个工单，实际同时在跑的最多 4 个。

Java 对照：`java.util.concurrent.Semaphore`，一模一样。区别是这里等待的是协程，几乎不占资源。

### 4.3 `asyncio.wait_for` —— 超时并且真的取消

```python
result = await asyncio.wait_for(spec.fn(workspace, **kwargs), timeout=timeout or self._timeout)
```

超时会做两件事：**取消**里面那个协程，然后抛 `TimeoutError`。

**和 Java 的关键区别**：`Future.get(timeout)` 只是**你不等了**，那个任务还在后台继续跑、继续占资源。`wait_for` 是真的把它取消掉。更接近 `future.cancel(true)`。

⚠️ **但是**：取消一个协程**不会杀掉它启动的子进程**。这就是为什么 `sandbox/local.py` 必须自己处理进程组 —— 见 §7。

本项目另一处 `wait_for` 的用法很妙：

```python
# src/repopilot/worker/worker.py:88
async def _sleep_or_stop(self, seconds: float) -> None:
    """睡一会儿，但收到停机信号立刻醒 —— 别让停机等满一个轮询周期。"""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
```

读法：「**等停机信号**，最多等 `seconds` 秒」。

- 正常情况：没人喊停 → 等满超时 → 抛 `TimeoutError` → 被 `suppress` 吃掉 → 效果 = 睡了 1 秒
- 停机时：`_stopping` 被 set → **立刻返回**，不用等满

用 `asyncio.sleep(1)` 也能睡，但停机时要傻等最多 1 秒。**这个写法把"轮询退避"和"响应停机信号"合成了一件事。**

### 4.4 `asyncio.Event` —— 一个开关

```python
self._stopping = asyncio.Event()

self._stopping.set()          # 拉闸（stop() 里）
self._stopping.is_set()       # 查状态（while 条件里）
await self._stopping.wait()   # 等到被拉闸为止
```

Java 对照：`CountDownLatch(1)`。

### 4.5 `asyncio.Queue` —— 协程间传数据

```python
# src/repopilot/worker/bus.py:37
queue: asyncio.Queue = asyncio.Queue()
queue.put_nowait(event)       # 放，不等（队列无界所以永不阻塞）
item = await queue.get()      # 取，空了就挂起等
```

`put_nowait` 和 `put` 的区别：有界队列满了时，`put` 会等，`put_nowait` 会抛异常。这里队列无界，所以用 `put_nowait` 让 `publish()` 可以是**同步函数** —— 事件发布方不需要是 async，写起来轻松很多。

SSE 那条链就是靠它：`Runner.emit()` 往队列里 `put_nowait`，HTTP 处理函数在另一头 `await queue.get()`。

Java 对照：`LinkedBlockingQueue`。

### 4.6 `asyncio.wait` —— 等一批，带超时

```python
# src/repopilot/worker/worker.py:97
done, pending = await asyncio.wait(self._inflight, timeout=self.settings.shutdown_grace_seconds)
for task in pending:
    task.cancel()
```

和 `gather` 不同：`wait` **不抛异常**、**支持超时**、返回 `(完成的, 还没完成的)` 两个集合。

优雅停机的核心三行：等最多 30 秒 → 还没完的强制取消 → 这些任务的租约会过期，被别的 worker 接手。

---

## §5 取消（cancellation）：本项目最精巧的一段

`task.cancel()` 会在那个协程**当前挂起的 `await` 处**抛出 `asyncio.CancelledError`。

关键理解：**取消不是立刻杀死，是往里面扔一个异常**。所以协程有机会做清理（`finally` 块会执行）。

看 `Runner.execute` 怎么处理它：

```python
# src/repopilot/worker/runner.py:123
except asyncio.CancelledError:
    # 不改状态：租约会过期，任务自然回到队列被别人接手
    emit("run_error", message="worker 被取消，任务将由租约回收")
    raise                       # ← 必须重新抛出！
```

两个要点：

1. **`raise` 必须写。** 吞掉 `CancelledError` 会让上层以为任务正常完成，事件循环的取消机制就废了。ruff 也会警告。
2. **故意不改数据库状态。** 这里如果写成 `failed`，任务就永久死了。什么都不做的话，run 保持 `running`、租约到期 → 别的 worker 用 `CLAIM_SQL` 把它捞回来重试。**"什么都不做"是一个经过设计的决定，不是遗漏。**

对比一下三条退出路径的处理，这是整个 Runner 的精华：

| 情况 | 处理 | 为什么 |
|---|---|---|
| Agent 跑完，测试通过 | → `pending_approval` | 成功也不能直接发布，卡在审批闸门 |
| Agent 跑完，测试没过 | → `failed` | 是**业务失败**，重试也没用（预算已经在图内部用完了） |
| 进程被取消 / 崩溃 | **不动状态** | 是**基础设施失败**，租约会回收重试 |
| 其他异常 | 还有次数 → `queued`，否则 `failed` | 见 `_fail()` |

**面试口径**：
> "我把'任务失败'和'执行器失败'分开处理。前者是终态，重试没意义；后者不改状态，靠租约超时让别的 worker 接手。这个区分在消息队列里对应的就是 nack 和 ack 超时重投的差别。"

---

## §6 阻塞调用会毒死整个事件循环

再强调一次：**一个服务员**。他去洗盘子，十张桌子全等着。

```python
time.sleep(5)          # ❌ 整个进程冻结 5 秒，所有 run、所有 HTTP 请求全卡
await asyncio.sleep(5) # ✅ 只有当前协程等，别人照跑

requests.get(url)      # ❌ 阻塞
await httpx.AsyncClient().get(url)   # ✅

subprocess.run(cmd)    # ❌ 阻塞
await asyncio.create_subprocess_exec(*cmd)   # ✅ ← 本项目用的
```

`sandbox/local.py` 用 `create_subprocess_exec` 而不是 `subprocess.run`，就是这个原因：pytest 可能跑 60 秒，用同步版本会让 API 60 秒无响应。

### 那文件读写呢？

```python
# src/repopilot/tools/fs_tools.py:106
text = path.read_text(encoding="utf-8")     # 这是同步的！
```

**是的，这里是同步阻塞的，而且是有意的取舍。** 本地文件读写通常是微秒级（还有 page cache），阻塞时间可以忽略；换成异步文件 I/O 要引入 `aiofiles` 依赖、代码复杂一倍，收益不成正比。

**但要能说出这个取舍**，因为面试官可能会挑这个：
> "文件 I/O 我用的是同步 API。理由是本地小文件读写是微秒级、有 page cache，阻塞时间远小于一次事件循环调度的开销。如果之后要读大文件或者走网络文件系统，会换成线程池（`asyncio.to_thread`）或者 `aiofiles`。"

顺带一提，ruff 的 `ASYNC` 规则组会**自动检查**这类问题（`ASYNC240`：async 函数里用阻塞的 `pathlib` 操作）。本项目为此专门抽了两个同步辅助函数：

```python
# src/repopilot/db/pool.py:77
def read_schema(schema_file: Path) -> str:
    """同步读一次 DDL 文件，别在协程里做阻塞 IO。"""
    return schema_file.read_text(encoding="utf-8")

# src/repopilot/api/routes.py:255
def _resolve_repo_path(raw: str | None, settings: Settings) -> Path:
    """同步函数：两次 stat 调用，别放在 async 处理函数里。"""
```

**把阻塞操作从协程里抽出来变成一个显式的同步函数** —— 阻塞还是阻塞了，但它现在是**可见的、有名字的、有注释解释的**，而不是藏在一大段 async 代码里。

---

## §7 取消协程 ≠ 杀死子进程（这是个真实的坑）

```python
# src/repopilot/sandbox/local.py:48
proc = await asyncio.create_subprocess_exec(
    *command,
    cwd=str(cwd),
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env={**os.environ, **(env or {})},
    start_new_session=True,          # ← 关键
)
```

`start_new_session=True` = 把子进程放进**它自己的进程组**。

为什么需要：pytest 自己会 fork 子进程。如果只 kill 直接子进程，那些孙子进程会变成孤儿，继续跑、继续占着 workspace 目录、继续吃 CPU。

于是超时处理是这样的：

```python
try:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
except TimeoutError:
    timed_out = True
    _kill_process_group(proc.pid)             # ← 杀整个进程组
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    except TimeoutError:
        stdout, stderr = b"", b""

def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
```

逐行讲：

- `os.getpgid(pid)` → 拿到进程组 ID
- `os.killpg(pgid, SIGKILL)` → 给**整组**发 KILL 信号，孙子进程一起死
- 抓 `ProcessLookupError` → 进程可能刚好自己退出了，这不是错误
- 杀完**再 communicate 一次（5 秒）** → 把管道里已经产生的输出捞出来。有输出的超时报告比空白的有用得多。
- 第二次还超时 → 认输，返回空 bytes

**面试口径**（这一段含金量很高）：
> "wall-clock 超时只是第一层。asyncio 的取消只作用于协程，管不到操作系统进程。所以我用 `start_new_session=True` 让子进程独立成组，超时时 `killpg` 整组清理，避免 pytest 的孙子进程变孤儿。杀完还会再收一次输出，因为超时报告里有没有 stack trace 决定了排查效率。"

---

## §8 `ContextVar` —— asyncio 版的 ThreadLocal

```python
# src/repopilot/observability/logging.py:11
run_id_var: ContextVar[str] = ContextVar("run_id", default="-")

class _RunIdFilter(logging.Filter):
    def filter(self, record):
        record.run_id = run_id_var.get()      # 每条日志自动带上当前 run_id
        return True
```

用的地方：

```python
# src/repopilot/worker/runner.py:39
token = run_id_var.set(str(row.id)[:8])
...
finally:
    run_id_var.reset(token)                   # 还原
```

设一次，之后**这个 run 里所有节点、所有工具、所有 SQL 层打的日志**，全部自动带上 `[run=a1b2c3d4]` 前缀，不用把 run_id 一路传进每个函数签名。

Java 对照：**SLF4J 的 MDC**，一模一样的模式。

**和 ThreadLocal 的关键差别**：`ThreadLocal` 在线程池里会串味（任务 A 留下的值被任务 B 读到，必须手动清）。`ContextVar` 是**每个 Task 创建时复制一份上下文**，所以两个并发的 run 各有各的 run_id，天然隔离。

`set()` 返回一个 `token`，`reset(token)` 用它还原到之前的值 —— 支持嵌套。

打开 `make run` 的日志你会看到：
```
2026-09-09 10:00:01 INFO  [run=a1b2c3d4] repopilot.tools.base | tool=read_file ok=True 3ms
```
那个 `run=a1b2c3d4` 就是这么来的。

---

## §9 `asyncio.run` vs uvicorn

事件循环得有人启动。两个入口：

```python
# scripts/demo.py
asyncio.run(main())        # 起一个事件循环，跑完 main 就关掉
```

```bash
make run                   # uvicorn 自己起事件循环，然后一直转
```

**不要在已经有事件循环的地方调 `asyncio.run`** —— 会报 "cannot be called from a running event loop"。

pytest 那边由 `asyncio_mode = "auto"` 配置接管：pytest-asyncio 看到 `async def test_xxx` 就自动给它起循环。所以本项目的测试可以直接写：

```python
async def test_something():
    result = await some_async_fn()
```

不用加任何装饰器。

---

## §10 这一章的面试口径（3~5 个）

**Q: 为什么用 asyncio 不用线程池？**
> 见 [01-python-kernel.md](01-python-kernel.md) §3。要点：I/O 密集 + GIL + 协程比线程便宜 + 切换点明确所以基本不用锁。

**Q: `await` 的时候在干什么？**
> 把控制权交还给事件循环，循环去跑别的就绪任务；等这个 I/O 有结果了，把当前协程重新放回就绪队列。整个过程一个线程、纯用户态切换。

**Q: 怎么限制并发？**
> 两层，是乘的关系。`Worker._slots`（Semaphore，默认 2）限制同时跑几个 Agent；`ToolRegistry._semaphore`（默认 4）限制单个 Agent 内同时几个工具。最坏 8 个工具并发。放在注册表而不是调用点，是为了让新增工具自动继承上限。

**Q: 超时怎么做的？取消能保证资源释放吗？**
> `asyncio.wait_for` 包住每个工具调用，超时会真的取消协程。但取消只作用于协程 —— 子进程管不到，所以 sandbox 里用 `start_new_session=True` + `os.killpg` 杀整个进程组。这两层缺一不可。

**Q: worker 被强杀，正在跑的任务怎么办？**
> 三种情况分开处理。优雅停机：先停止领新任务，等最多 30 秒，超时的 `task.cancel()`；被取消的 run **故意不改数据库状态**，让租约到期后被别的 worker 用 `SKIP LOCKED` 重新领走。`kill -9`：没人续租，租约照样过期，同样被回收。重试次数用尽的由 reaper 标记为 failed，防止无限打转。

---

下一章：[04-stack.md](04-stack.md) —— Pydantic / FastAPI / PostgreSQL / LangGraph 四个框架。
