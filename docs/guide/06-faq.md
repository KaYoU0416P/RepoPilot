# 06 · 排错手册 + 面试问答口径

---

# 第一部分：排错手册

## E1. `ModuleNotFoundError: No module named 'repopilot'`

**最常见的一个，而且这台机器上会反复出现。**

原因：macOS 的 `UF_HIDDEN` 标志 + CPython 静默跳过隐藏的 `.pth` 文件（详见 [01-python-kernel.md](01-python-kernel.md) §6）。

**触发时机：每次 `uv add` / `uv sync` 之后。**

诊断：
```bash
uv run python -c "import sys; print('_virtualenv' in sys.modules)"
# False → 所有 .pth 都被跳过了，就是这个问题
```

解法：
```bash
make sync
```

**永远不要直接敲 `uv sync`。**

注意 `pytest` 因为有 `pythonpath = ["src"]` 兜底所以不受影响，**但 `make run`（uvicorn）会炸**。所以"测试能过但服务起不来"就是这个症状。

---

## E2. `连接池还没初始化，先调 init_pool()`

`get_pool()` 抛的。原因：在 FastAPI 的 lifespan 之外用了数据库函数。

常见场景：写了个脚本直接 `import` 仓储层就调。解法：脚本里自己先 `await init_pool(settings.database_url)`。

---

## E3. `connection refused` / 连不上数据库

```bash
docker ps | grep repopilot-pg      # 容器在跑吗
make db-up                          # 起容器，并等到真的能连
```

如果容器在跑还是连不上，检查端口：**是 5433 不是 5432**。

---

## E4. 改了 `db/schema.sql` 但表结构没变

`docker-entrypoint-initdb.d` **只在数据目录为空时执行**，也就是只有第一次启动生效。

```bash
make db-reset        # docker compose down -v（删卷）+ 重新起
```

⚠️ 这会**删掉所有数据**。

---

## E5. 测试失败：`InvalidTransition: 不能从 X 变成 Y`

这通常不是 bug，是**状态机在正常工作**。检查你是不是想跳过中间状态。

正确的做法是沿着合法路径一步步走。`tests/test_api.py` 里的 `_force_status` 就是这么写的：

```python
_HAPPY_PATH = [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PENDING_APPROVAL,
               RunStatus.PUBLISHING, RunStatus.PUBLISHED]

async def _force_status(run_id: str, target: RunStatus) -> None:
    uid = UUID(run_id)
    stop = _HAPPY_PATH.index(target)
    while True:
        row = await runs_repo.get_run(uid)
        current = _HAPPY_PATH.index(row.status)
        if current >= stop:
            return
        await runs_repo.transition(uid, _HAPPY_PATH[current + 1])   # 一步一步走
```

---

## E6. 数据库相关的测试被跳过了

`tests/conftest.py` 里有个钩子：**5433 端口连不上就自动跳过所有需要数据库的测试**，不让整个测试套件红掉。

```bash
make db-up && make test
```

只想跑不需要数据库的：`make test-nodb`。

---

## E7. `RuntimeError: asyncio.run() cannot be called from a running event loop`

在已经有事件循环的地方又调了 `asyncio.run`。在 async 函数里应该直接 `await`。

---

## E8. `coroutine was never awaited` 警告

调了 `async def` 的函数但忘了 `await`（见 [03-asyncio.md](03-asyncio.md) §2.1）。

```python
runs_repo.get_run(uid)          # ❌ 只造了个协程对象
await runs_repo.get_run(uid)    # ✅
```

---

## E9. ruff 报错

```bash
uv run ruff check .            # 看问题
uv run ruff check --fix .      # 能自动修的自动修
uv run ruff format .           # 格式化
```

本项目已经 `ignore` 的两条，以及**为什么**：
- `B008`（别在参数默认值里调函数）—— FastAPI 的 `Depends()` 就是这么用的
- `ASYNC109`（别自定义 `timeout` 参数）—— 我们的 `run_command`、`registry.call` 就是要有这个参数

`ASYNC240`（async 函数里的阻塞 pathlib 调用）**没有 ignore**，而是抽了同步辅助函数解决（`read_schema`、`_resolve_repo_path`）。

---

## E10. VSCode 没有代码补全 / 到处飘红

`.vscode/settings.json` 里已经配好了：

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.analysis.extraPaths": ["${workspaceFolder}/src"]
}
```

如果还不行：`Cmd+Shift+P` → `Python: Select Interpreter` → 选 `.venv/bin/python`。

要装的插件：**Python**（微软官方）、**Ruff**（charliermarsh）。`.vscode/extensions.json` 里有推荐。

---

## E11. Docker 拉镜像失败

已知问题，Docker Hub 经常拉不动。本项目已经改用本机已有的 `pgvector/pgvector:0.8.6-pg17-trixie`。

```bash
docker images | grep pgvector      # 确认本机有这个镜像
```

---

# 第二部分：面试问答口径

按被问的概率排序。**每条都是可以直接说出口的完整句子**，不是提纲。

## 一、项目层面

### Q1: 一分钟介绍这个项目

> RepoPilot 是一个受控的代码修改 Agent 服务。核心不是"让 AI 改代码"这件事本身，而是**怎么把一个不可靠的执行体，包进一套可靠的后端工程里**。
>
> 具体来说：任务落库后才返回 202，保证不丢；worker 用 `FOR UPDATE SKIP LOCKED` 加租约的方式领取，保证并发安全且崩溃可恢复；Agent 在仓库副本里工作，路径收敛加子进程超时，保证不越界；用测试结果判定成败而不是听 Agent 自称；成功的产物停在人工审批闸门，批准后才发布。
>
> 技术栈是 FastAPI + LangGraph + asyncio + PostgreSQL，118 个测试。

### Q2: 为什么做这个项目？

> 我之前有一个 Java 的 Agentic RAG 项目，覆盖了检索和工具调用。这次我想补的是另一半：**Agent 的运行时和它的工程约束** —— 沙箱、预算、审批、可观测性。而且我想验证一件事：AI Agent 领域里真正稀缺的不是提示词，是把它接进真实流程的那套后端功夫，那恰好是我的强项。

### Q3: 最难的部分是什么？

（挑一个真实的讲，别说"提示词调优"。）

> 有两个。一个是**状态机和并发的交互设计** —— 一开始我用应用层的 if 判断状态流转，后来意识到读到写之间有并发窗口，才改成表驱动的守卫加上数据库层用 `status` 当版本号的乐观锁。
>
> 另一个是排查一个环境问题：包装上了、`uv pip list` 显示正常，但 `import` 就是失败。最后定位到是 macOS 的隐藏文件标志导致 CPython 静默跳过了 `.pth` 文件。这个我写进了 `docs/failures.md`。

---

## 二、并发与异步

### Q4: 为什么用 asyncio 而不是线程池？

> Agent 的工作全是 I/O 密集的：文件读写、跑测试的子进程、数据库、LLM 的 HTTP 调用，CPU 几乎不干活。这种场景要的是并发不是并行，一个事件循环就能把吞吐打满。而且 CPython 有 GIL，多线程在 CPU 密集场景本来也没有加速，反而多了线程栈内存和加锁成本。
>
> 附带的好处是心智负担小：协程只在 `await` 处切换，两个 `await` 之间是原子的，所以像事件序号自增这种操作不需要加锁。

### Q5: 怎么控制并发？

> 两层，是**乘**的关系。第一层在 worker 上，一个 `Semaphore` 限制同时跑几个 Agent，默认 2；第二层在工具注册表上，限制单个 Agent 内同时几个工具调用，默认 4。最坏情况是 8 个工具并发。
>
> 关键是第二层**放在注册表而不是每个工具里** —— 这样新加一个工具自动继承上限，作者不用记得加。
>
> 还有一个细节：worker 是**先拿并发令牌，再去数据库领任务**。反过来的话，任务已经从队列里捞出来、状态改成 running、租约开始计时了，却在内存里干等空位。这就是 MQ 里 prefetch 的道理。

### Q6: 超时怎么做的？取消能保证资源释放吗？

> `asyncio.wait_for` 包住每个工具调用，超时会**真的取消**里面的协程 —— 这一点和 Java 的 `Future.get(timeout)` 不同，那个只是你不等了，任务还在跑。
>
> 但取消只作用于协程，**管不到操作系统进程**。pytest 会 fork 子进程，只杀直接子进程会留下孤儿。所以 sandbox 里用 `start_new_session=True` 把子进程放进独立进程组，超时时 `os.killpg` 杀整组。杀完还会再收 5 秒输出 —— 因为超时报告里有没有 stack trace，决定了排查效率。

---

## 三、数据库与可靠性

### Q7: 为什么用数据库表当队列，不用 MQ？

> 三个理由。第一，任务本身就是业务实体 —— 要查询、要审批、要展示历史，本来就得落库。第二，队列和业务表是同一张表，**入队和业务写入天然在一个事务里，不存在 MQ 和 DB 双写不一致**。第三，量级不需要 —— 任务是分钟级的。
>
> 该换 MQ 的信号是：吞吐到万级 TPS、需要扇出或多消费组、需要跨服务解耦。这个取舍我写在 `docs/learning.md` 里了。

### Q8: 领取任务的 SQL 讲一下

（把 [05-happy-path.md](05-happy-path.md) 站 5.2 讲一遍。核心几句：）

> 一条 `UPDATE`，`WHERE id` 等于一个内层子查询的结果，最后 `RETURNING *`。
>
> 内层选出可领取的行：重试次数没用完，且状态是 `queued`，**或者**状态是 `running` 但租约已过期 —— 第二个分支就是崩溃恢复。加 `FOR UPDATE SKIP LOCKED`，让并发的 worker 跳过已被锁住的行各拿各的，而不是排队等锁退化成串行。
>
> 外层把状态改成 running、attempts 加一、写入 `locked_by` 和租约到期时间，`started_at` 用 `COALESCE` 保证只在首次领取时写。**选中和标记在同一条语句里完成，中间没有任何窗口**。
>
> 有一点我要说清楚：`SKIP LOCKED` **MySQL 8.0 也有**，不是 PG 独占。PG 在这个场景真正的优势是 `RETURNING`（一条语句完成领取和取数据，MySQL 得再查一次而且中间有并发窗口）和**部分索引**（只索引待领取的行，队列表 99% 是历史数据，MySQL 完全没这个功能）。

### Q9: worker 崩了任务会丢吗？

> 不会。领取时写的不是一把锁，是一个**租约** —— 一个到期时间戳。执行期间后台协程每 40 秒续租一次，租约是 120 秒，也就是允许连续丢两次心跳。
>
> worker 被 `kill -9` 就没人续租了，租约到期后任务被别的 worker 用刚才那条 SQL 的第二个分支捞回来重试。优雅停机时被取消的 run **故意不改数据库状态**，走的是同一条恢复路径。重试次数用尽的由一个 reaper 协程标记为 failed，防止无限打转。
>
> 这套机制对应的就是 RabbitMQ 的 ack 超时重投加死信队列，或者 Redisson 的看门狗续期。区别是这里的"锁"只是数据库里一个时间戳字段，没引入任何中间件。

### Q10: 续租有什么坑？

> 续租的 SQL 必须带 `AND locked_by = 我自己`。
>
> 场景是：我这个 worker 卡了三分钟（GC 停顿或者机器挂起），租约过期，任务被 worker-B 领走了。现在我醒了要续租 —— 如果不验证持有者，我会把到期时间又往后推，结果**两个 worker 同时跑同一个 run**。
>
> 加了这个条件，我的 UPDATE 匹配 0 行，`execute` 返回 "UPDATE 0"，我就知道自己失去所有权了，停止续租。这是分布式锁最经典的坑，Redisson 释放锁时也要验证 value 是不是自己的 UUID。

### Q11: 幂等怎么做的？

> `webhook_deliveries` 表，`delivery_id` 是主键，用 GitHub 的 `X-GitHub-Delivery` header 当幂等键 —— 同一个事件重投时它不变。
>
> SQL 是 `INSERT ... ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id`。冲突时零行返回，所以 Python 侧 `record is None` 就等于重复投递。**判断和登记是同一条语句，天然原子。**
>
> 先 SELECT 再 INSERT 是经典错误 —— N 个并发请求会同时读到"不存在"，然后都以为自己是第一个。**唯一约束是唯一可靠的仲裁者。** 我有个测试用 20 个并发协程专门打这个点。
>
> MySQL 对照是 `INSERT IGNORE`，效果类似但**没有 RETURNING**，得再查一次才知道是不是自己插进去的。

### Q12: 状态机为什么要表驱动？

> 把"谁能变成谁"写成一张 `dict[状态, frozenset[状态]]`，而不是散落各处的 if。三个好处：非法流转在一个地方被挡住；这张表本身可以被测试（我用 BFS 验证每个活跃状态都能到达终态，防死角）；**终态是从表推导的**（没有出边的状态），不手写第二份清单。
>
> 落到数据库是两层保护，缺一不可：应用层的 `assert_transition` 挡业务上非法的流转，数据库层的 `UPDATE ... WHERE status = 当前状态` 是乐观锁，挡读到写之间被人改了。第二层等价于 JPA 的 `@Version`，只是这里**用 status 本身当版本号**。
>
> 顺带一提，**自转是非法的** —— 因为 status 兼任版本号，允许原地踏步会让乐观锁失去意义。这是一个从实现约束反推出来的业务规则。

---

## 四、Agent 与安全

### Q13: 怎么防止 Agent 干坏事？

> Sandbox 不是一个功能，是三个不同的风险配三个不同的措施：
>
> 一、**写错路径**（比如 `../../.ssh/authorized_keys`）→ 每个模型给的路径都过 `Workspace.resolve()`，它先 `.resolve()` 规范化并跟随符号链接，再检查结果是否还在 workspace 根目录下面。绝对路径、`..` 穿越、指向外面的软链接全部挡住。
>
> 二、**生成的代码不终止** → 墙钟超时加 `os.killpg` 杀整个进程组。
>
> 三、**改坏真实仓库** → 全程操作 `copytree` 出来的副本，真实仓库一个字节都不碰。
>
> 另外还有一条更重要的：**我刻意没有提供通用 shell 工具**。有了 shell，上面三条全是装饰品。唯一的执行类工具是 `run_tests`，命令行是写死的列表，而且用的是 `create_subprocess_exec` 不是 `_shell` —— 命令注入这一整类问题从根上不存在。

### Q14: 怎么判断 Agent 真的成功了？

> 三重确认，而且都不听 Agent 自称。
>
> 节点层：测试通过**并且**确实改了文件。第二个条件很关键 —— 只看测试的话，"什么都不做"是最容易通过的策略，因为有些测试本来就是绿的。
>
> 评估层：再加一条 diff 非空且不等于 "(no changes)"。
>
> 状态机层：就算前面都判成功了，**`running` 到 `published` 也没有直达的边**，必须经过 `pending_approval` 让人看。这一条约束就是审批闸门的全部实现 —— 不是靠代码里记得检查，是靠结构上不允许。

### Q15: 重试怎么设计的？

> 两个不同层次的重试，我在字段上就分开了。
>
> `attempts` 是**基础设施层**的：任务被 worker 领取了几次。崩溃、超时、进程被杀会消耗它，上限 3 次，用尽由 reaper 标记失败。
>
> `retry_count` 是**业务层**的：Agent 在一次执行里重跑 execute 节点的次数，上限 2 次（也就是最多 3 次尝试）。
>
> 关键是重试要**有信息增量** —— 我把上一次失败的测试输出（取最后 3000 字符，因为错误信息在末尾）拼进下一轮的提示词。这是唯一让第 N+1 次和第 N 次不同的东西。没有这个反馈，重试三次只会得到三个一模一样的错误答案。

### Q16: 怎么拿到模型的结构化输出？

> 我不解析自然语言。把 Pydantic 模型用 `model_json_schema()` 转成 JSON Schema，当成一个"工具"的入参声明发给模型，然后用 `tool_choice` **强制**它必须调用这个工具。这样结构约束在服务端就完成了，回来我再用 `model_validate` 校验一次，双保险。
>
> 好处是节点里永远没有字符串解析，拿到的直接是 `Analysis`、`Plan` 这种有类型的对象；schema 不符会在**边界处**抛一个 `LLMError`，而不是三个节点之后抛 `AttributeError`。
>
> 而且一个 Pydantic 类同时是三样东西：数据结构、发给模型的 schema、收回来的校验器。**一处定义三处生效。** Java 对照是 Spring AI 的 `BeanOutputConverter`，区别是那个是客户端解析。

### Q17: 为什么用 LangGraph 不用 while 循环？

（诚实版，别吹。）

> 好处是重试路径、预算检查、退出条件是**声明成边的**，也就是数据 —— 可检视、可流式输出、可单独测试。我的路由函数就是个纯函数，测试里直接当普通函数调，不用起整个图。流式输出和检查点也是白送的。
>
> 但**诚实说**，就这个"六节点线性加一条重试边"的流程而言，while 循环也能写，而且更短。图的收益在于加分支的时候 —— 比如加人工介入节点、加多个执行器。我选它也有学这个范式的成分。

### Q18: LangGraph 的 state 怎么合并的？

> state 是个 `TypedDict`，节点返回的是**增量**不是全量，框架负责合并。合并语义**按 key 声明在类型上**：用 `Annotated[list[X], operator.add]` 标注的键做拼接，其余的覆盖。
>
> 所以工具调用记录、错误、步骤日志这三个累积型字段，跨三次重试自动累加，节点不需要读-改-写，两个节点也不会互相覆盖。
>
> 至于为什么 state 用 `TypedDict` 不用 Pydantic：`TypedDict` 运行时就是个普通 dict，零开销零校验，而 LangGraph 内部就是当 dict 合并的。这些数据不来自外部，不需要校验。**原则是边界上用 Pydantic 校验，内部用 TypedDict 保持轻快。**

---

### Q19: 审批通过之后怎么开 PR 的？为什么不在审批那个请求里直接发？

> 审批只把状态改成 `publishing`，真正发布的是**第二条并排的循环**。三个理由：开 PR 要走网络可能几秒到超时，HTTP 请求不该等它；审批的人点完就该走，发布失败不能变成"批准失败"；单独一个循环才能享受同一套租约机制，发布到一半崩了会被自动重试。
>
> **这和 `POST /runs` 只入队不执行是同一个原则，只是换了个阶段。**
>
> 另外有个细节：审批时会把租约和 `locked_by` 清空，而且**和状态流转写在同一条 UPDATE 里**。分成两条语句的话，中间那一瞬间状态已经是 `publishing`、租约还在前一个 worker 手上，publisher 捞不到，得干等一整个租约周期。

### Q20: 发布这一步的幂等怎么做的？

> 和 webhook 那边不一样，因为**权威方不同**。webhook 的权威是我自己的数据库，所以用唯一约束；发布的权威是 GitHub，我控制不了它的表，所以用**确定性命名 + 查重**。
>
> 分支名是 `repopilot/run-<run_id前8位>`，同一个 run 永远算出同一个名字。push 同样的提交在 git 层面是 no-op；开 PR 之前先查"这个 head 分支上有没有开着的 PR"，有就复用。所以最难缠的那个中间态 —— push 成功了但开 PR 之前崩了 —— 重试时会被捡回来，不会开出第二个 PR。
>
> 还有一点：**发布只依赖数据库里的 diff，不依赖执行时的 workspace**。因为审批闸门意味着执行和发布之间隔着任意长的人类时间，中间进程重启过、临时目录被清理过都很正常。发布是重新 clone 一份把 diff 打上去，可以在任何一台机器上重放。

### Q21: 发布失败了怎么办？

> 严格分两类，和 worker 那边是同一个哲学。
>
> `PublishError` —— diff 打不上、仓库不存在、token 没权限，也就是 GitHub 返回 4xx。重试多少次结果都一样，直接标 `failed` 进终态等人来看。
>
> 其他异常 —— 网络抖动、5xx、429 限流。**故意不 catch**，让它冒到循环外面，租约不续 → 过期 → 下一轮自动重新领取。因为上面那套幂等保证重试是安全的。
>
> 注意 **429 虽然是 4xx，但我把它归到"该重试"那一类** —— 它的语义是"待会再来"，不是"你错了"。
>
> Java 对照就是死信队列和重新投递的区别。

---

## 五、必须主动说的已知缺口

**面试前把这一节背熟。主动说出来是加分，被问出来是减分。**

### 1. ScriptedLLM 不是 Agent

> 项目默认跑在一个叫 `ScriptedLLM` 的**确定性测试替身**上，它只认识内置的样例仓库，是硬编码的规则。这样做是为了让测试不联网、不花 token、结果可复现。配了 API key 就会走真实模型。
>
> **我说清楚这一点，是因为不说的话演示效果会有误导性。**

### 2. 事件总线是进程内的

> SSE 用的事件总线是进程内的。API 和 worker 拆成两个进程后就收不到事件了，要拆得换 Redis pub/sub 或者 PG 的 `LISTEN/NOTIFY`。
>
> 但**业务正确性不受影响** —— 真相在数据库里，客户端轮询 `GET /runs/{id}` 结果一样。SSE 只是实时展示的优化。

### 3. 队列靠轮询，不是零延迟

> worker 空转时每秒轮询一次，最坏有 1 秒延迟。PG 的 `LISTEN/NOTIFY` 能做到零延迟，但会让代码复杂一截，MVP 阶段接受这个取舍。

### 4. sandbox 是子进程不是容器

> 现在的隔离是路径收敛加超时，**不是内核级隔离**。恶意代码理论上还能读到 workspace 之外的文件（虽然写不进去）、还能发网络请求。
>
> 接陌生仓库之前必须换成 Docker sandbox。我的 `run_command` 签名就是为这个留的 —— 换实现不动上层。

### 5. 超时层级有个已知的不一致

> `run_tests` 传的是 `timeout=None`，会用注册表的 20 秒默认值，但工具内部还有自己的 60 秒。**外层会先触发**，慢测试套件会报"tool timed out"而不是"tests timed out"。对当前的样例仓库没影响，接真实仓库前要修。

### 6. Runner 里镜像了一份 reducer

> `astream` 只给增量，我需要完整 state 来做评估和落库，所以在 `runner._merge` 里镜像了一遍 reducer 逻辑。这和 `state.py` 里的 `Annotated` 声明是**重复的**，不同步就会出 bug。更好的做法是用 `stream_mode="values"` 拿全量。这是我知道但暂时没改的技术债。

### 7. API 没有鉴权

> 目前没有任何认证授权，只能内网跑。

### 8. 没配 GitHub token 时发布是空转的

> `build_publisher()` 在没有 `github_token` 时返回 `DryRunPublisher`，`pr_url` 留空表示**没真的发出去**。
>
> 这和 webhook 验签的 fail closed 标准不同，是有意的：**验签是安全边界，缺配置必须拒绝；发布是功能，缺配置降级并且在结果里说清楚。**

### 9. 数据库挂了没有兜底

> 所有操作会抛异常，`/health` 会显示 degraded，但没有降级策略。

### 10. 没有 ORM，加字段要改两处

> `schema.sql` 和 `db/models.py`。这是有意的取舍 —— 我想真的学 PG 的写法，而且核心那条 `FOR UPDATE SKIP LOCKED` 在 ORM 里写出来会遮住重点。代价是维护两份字段清单。

---

# 第三部分：速查

## 命令

```bash
make sync      # 装依赖（永远用这个）
make db-up     # 起 PG（会等到真的能连）
make test      # 244 passed
make test-nodb # 不需要数据库的那部分
make demo      # 单跑一次 Agent，不起服务不用 key
make run       # uvicorn :8000，/docs 有 Swagger
make psql      # 数据库命令行
make db-reset  # 改了 schema.sql 之后删库重建
```

## 完整业务链路演示（面试可以现场跑）

```bash
RID=$(curl -s -X POST localhost:8000/runs -H 'content-type: application/json' \
  -d '{"task":"Fix divide() so dividing by zero raises ValueError"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["run_id"])')

curl -sN localhost:8000/runs/$RID/events                      # SSE，每个节点一帧
curl -s  localhost:8000/runs/$RID | python3 -m json.tool      # → pending_approval

curl -s -X POST localhost:8000/runs/$RID/approval \
  -H 'content-type: application/json' \
  -d '{"decision":"approved","decided_by":"me","reason":"diff 看过了"}'   # → publishing

curl -s -X POST localhost:8000/runs/$RID/approval \
  -H 'content-type: application/json' -d '{"decision":"approved","decided_by":"me"}'
# → 409，不能批准两次
```

## 关键文件位置

| 想看什么 | 去哪 |
|---|---|
| 状态机（唯一真相来源） | [src/repopilot/domain/status.py](../../src/repopilot/domain/status.py) |
| **领取任务的 SQL** | [src/repopilot/db/runs.py:55](../../src/repopilot/db/runs.py) `CLAIM_SQL` |
| **幂等的 SQL** | [src/repopilot/db/deliveries.py:28](../../src/repopilot/db/deliveries.py) |
| 状态流转 + 乐观锁 | [src/repopilot/db/runs.py](../../src/repopilot/db/runs.py) `transition` |
| 领取待发布 + 交还租约 | [src/repopilot/db/runs.py](../../src/repopilot/db/runs.py) `CLAIM_PUBLISHING_SQL` / `RELEASE_LEASE` |
| **发布：clone + apply + push + 开 PR** | [src/repopilot/publishing/github.py](../../src/repopilot/publishing/github.py) |
| 发布循环 + 失败分类 | [src/repopilot/worker/worker.py](../../src/repopilot/worker/worker.py) `publish_once` |
| **webhook 验签（常数时间比较）** | [src/repopilot/github/webhook.py](../../src/repopilot/github/webhook.py) |
| 建表 DDL | [db/schema.sql](../../db/schema.sql) |
| 领取循环 + 限流 | [src/repopilot/worker/worker.py](../../src/repopilot/worker/worker.py) |
| 租约心跳 + 三条退出路径 | [src/repopilot/worker/runner.py](../../src/repopilot/worker/runner.py) |
| 路径收敛（安全核心） | [src/repopilot/workspace/manager.py:29](../../src/repopilot/workspace/manager.py) |
| 子进程沙箱 + killpg | [src/repopilot/sandbox/local.py](../../src/repopilot/sandbox/local.py) |
| 工具护栏（限流/超时/异常） | [src/repopilot/tools/base.py:70](../../src/repopilot/tools/base.py) |
| 六个节点 | [src/repopilot/agent/nodes.py](../../src/repopilot/agent/nodes.py) |
| 建图 + 条件边 | [src/repopilot/agent/graph.py](../../src/repopilot/agent/graph.py) |
| 结构化输出 | [src/repopilot/llm/anthropic_client.py](../../src/repopilot/llm/anthropic_client.py) |
| HTTP 路由 + SSE | [src/repopilot/api/routes.py](../../src/repopilot/api/routes.py) |
| 启动/停机 | [src/repopilot/api/app.py](../../src/repopilot/api/app.py) |

## 如果只背五句话

1. **"这张 `runs` 表同时是业务实体和任务队列，所以入队和业务写入天然同事务，不存在双写不一致。"**
2. **"先拿并发令牌再去数据库领任务 —— 没能力处理就不要把消息从队列里取出来占着。"**
3. **"续租必须验证持有者，否则 GC 停顿之后会出现两个 worker 跑同一个任务。"**
4. **"幂等键的判重不能放在应用层，要放在数据库的唯一约束上 —— `ON CONFLICT DO NOTHING RETURNING`，判断和登记是同一条语句。"**
5. **"`running` 到 `published` 没有直达的边，必须经过 `pending_approval`。这一条约束就是审批闸门的全部实现。"**
