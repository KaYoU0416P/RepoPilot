# 04 · 技术栈全解：Pydantic / FastAPI / PostgreSQL+asyncpg / LangGraph

---

# §1 Pydantic —— 会在运行时真的校验的「DTO + 注解校验」

## 1.1 它解决什么问题

Python 的类型注解运行时不生效（见 [02-syntax.md](02-syntax.md) §3）。所以外部数据进来时你根本不知道它长什么样：

```python
body = await request.json()
task = body["task"]        # 万一没这个键？万一是个数字？万一是 null？
```

Pydantic 的做法：**声明一个类，字段带类型，构造它的时候自动校验 + 转换**。

```python
# src/repopilot/api/schemas.py:13
class CreateRunRequest(BaseModel):
    task: str = Field(min_length=3, description="要 Agent 做的修改")
    repo_path: str | None = Field(default=None, description="仓库绝对路径，不填用内置样例")
    max_attempts: int | None = Field(default=None, ge=1, le=5)
```

- `task` 必填、必须是字符串、至少 3 个字符
- `repo_path` 可以不传，不传就是 `None`
- `max_attempts` 可以不传，传了必须在 1~5 之间

**Java 对照**：一个 DTO + `@NotNull @Size(min=3)` + Hibernate Validator，但这里是内置的、零配置的。

`Field(...)` 除了校验，`description` 还会进 OpenAPI 文档 —— 你在 `/docs` 里看到的说明文字就是这么来的。

## 1.2 关键 API

```python
CreateRunRequest(task="修 bug")          # 构造 = 校验，不合法直接抛 ValidationError
RunRow.model_validate(dict(record))       # 从字典构造（本项目从数据库行构造用这个）
report.model_dump()                       # 转成 dict
approval.model_dump(mode="json")          # 转成 dict，且把 UUID/datetime 转成字符串
item.model_dump_json()                    # 直接转 JSON 字符串（SSE 用这个）
schema.model_json_schema()                # 生成 JSON Schema（发给大模型用这个！）
```

`mode="json"` 那个区别很实际：普通 `model_dump()` 里 `UUID` 还是 `UUID` 对象，直接扔给 `json.dumps` 会炸；`mode="json"` 会转成字符串。

```python
# src/repopilot/api/routes.py:136
return [a.model_dump(mode="json") for a in await approvals_repo.history(run_id)]
```

## 1.3 `default_factory`

```python
meta: dict[str, Any] = Field(default_factory=dict)
files_changed: list[str] = Field(default_factory=list)
```

**必须这么写，不能写 `= {}`** —— 原因见 [02-syntax.md](02-syntax.md) §19 坑 1（可变默认参数共享）。

## 1.4 本项目三个 Pydantic 的使用场景

| 场景 | 文件 | 为什么 |
|---|---|---|
| HTTP 请求/响应 DTO | `api/schemas.py` | 外部输入不可信，必须校验 |
| 数据库行 → 对象 | `db/models.py` | 数据库返回的是 `Record`，校验一次转成有类型的对象 |
| **大模型输出的结构** | `agent/schemas.py` | 模型输出更不可信 |

第三个最有意思：

```python
# src/repopilot/agent/schemas.py:6
class Analysis(BaseModel):
    """Initial read of the task against the repository tree."""
    reasoning: str = Field(description="Short explanation of what the task requires.")
    relevant_files: list[str] = Field(default_factory=list, description="... Max 5.")
    search_queries: list[str] = Field(default_factory=list, description="... Max 3.")
```

这个类**同时是三样东西**：
1. Python 里的数据结构
2. 发给 Claude 的 **JSON Schema**（`model_json_schema()`）
3. 收到回复后的**校验器**（`model_validate()`）

一处定义，三处生效。**这是结构化输出的核心手法**，见 §1.6。

## 1.5 `BaseSettings` —— 配置从环境变量来

```python
# src/repopilot/config.py:12
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOPILOT_",       # 环境变量都以这个开头
        env_file=".env",               # 也从 .env 文件读
        extra="ignore",                # .env 里有不认识的键就忽略，别报错
    )

    llm_provider: Literal["anthropic", "scripted"] = "anthropic"
    max_concurrent_runs: int = 2
    lease_seconds: int = 120
    database_url: str = "postgresql://repopilot:repopilot@localhost:5433/repopilot"
```

所以环境变量 `REPOPILOT_MAX_CONCURRENT_RUNS=4` 会自动变成 `settings.max_concurrent_runs == 4`（**而且自动转成 int**）。

**Java 对照**：Spring Boot 的 `@ConfigurationProperties(prefix="repopilot")` + `application.yml`。一模一样的心智模型。

配合 `@lru_cache` 做单例（见 [02-syntax.md](02-syntax.md) §15）：

```python
@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if s.llm_provider == "anthropic" and not s.anthropic_api_key:
        s.llm_provider = "scripted"        # ← 没 key 自动降级，所以 make demo 不用配置就能跑
    return s
```

最后那两行是个体贴的设计：**没有 API key 时自动降级到测试替身**，新人 clone 下来直接能跑。

## 1.6 结构化输出：不解析文本，让模型"调用一个函数"

朴素做法是在提示词里写"请返回 JSON"，然后自己 `json.loads`。这个做法很脆：模型会加 ```json 围栏、会加解释文字、会漏字段。

本项目的做法：

```python
# src/repopilot/llm/anthropic_client.py:24
async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
    tool_name = _snake(schema.__name__)               # Analysis → analysis
    json_schema = schema.model_json_schema()          # Pydantic 类 → JSON Schema

    response = await self._client.messages.create(
        model=self._model,
        max_tokens=self._max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[{
            "name": tool_name,
            "description": schema.__doc__ or f"Return a {schema.__name__}",
            "input_schema": json_schema,              # ← 结构约束在这
        }],
        tool_choice={"type": "tool", "name": tool_name},   # ← 强制它必须调这个"工具"
    )

    for block in response.content:
        if block.type == "tool_use":
            try:
                return schema.model_validate(block.input)   # ← 再校验一次
            except ValidationError as exc:
                raise LLMError(f"{schema.__name__} validation failed: {exc}\ngot: {got}") from exc

    raise LLMError(f"model returned no tool_use block (stop_reason={response.stop_reason})")
```

思路：**声明一个"工具"，它的入参 schema 就是我要的数据结构，然后强制模型必须调用它。** 模型这时不能自由发挥，只能填这个结构。API 服务端会先帮你把形状约束住，回来我们再用 Pydantic 校验一次（双保险）。

好处：
- 节点里**永远没有字符串解析**，拿到的直接是 `Analysis` 对象
- 模型输出不符合 schema → 在**边界处**抛 `LLMError`，而不是三个节点之后抛 `AttributeError`

**Java 对照**：Spring AI 的 `BeanOutputConverter`，但那个是客户端解析，这个是服务端约束。

**面试口径**：
> "我不解析模型的自然语言输出。我把 Pydantic 模型转成 JSON Schema 当成一个工具的入参声明，用 `tool_choice` 强制模型必须'调用'它。这样结构约束在服务端完成，客户端再校验一次。一处定义，同时是数据结构、schema 和校验器。"

---

# §2 FastAPI

## 2.1 一分钟建立心智模型

| FastAPI | Spring Boot |
|---|---|
| `APIRouter()` | `@RestController` |
| `@router.post("/runs")` | `@PostMapping("/runs")` |
| 参数 `body: CreateRunRequest` | `@RequestBody CreateRunRequest body` |
| 参数 `run_id: UUID`（路径里有 `{run_id}`） | `@PathVariable UUID runId` |
| 参数 `status: RunStatus \| None = None` | `@RequestParam(required=false)` |
| `Depends(get_settings)` | `@Autowired` / 构造器注入 |
| `response_model=RunResponse` | 返回类型 |
| `HTTPException(409, ...)` | `@ResponseStatus` / 抛业务异常 |
| `lifespan` | `@PostConstruct` + `@PreDestroy` |
| 自动的 `/docs` | springdoc-openapi |

**核心魔法**：FastAPI 读你的**类型注解**来决定参数从哪来。

```python
@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID) -> RunResponse:
```

- 路径里有 `{run_id}` → 从路径取，**并且自动转成 UUID**，转不了直接返回 422
- 是 Pydantic 模型 → 从请求体取
- 是普通类型且路径里没有 → 从查询参数取
- 是 `Depends(...)` → 依赖注入

不用写任何注解，**类型就是配置**。这是 FastAPI 最大的卖点。

## 2.2 三个路由的完整解剖

### 简单的：GET

```python
# src/repopilot/api/routes.py:89
@router.get("/runs", response_model=list[RunResponse])
async def list_runs(
    status: RunStatus | None = None,       # 查询参数 ?status=queued，可不传
    limit: int = 50,                        # ?limit=100
    offset: int = 0,
) -> list[RunResponse]:
    rows = await runs_repo.list_runs(status=status, limit=min(limit, 200), offset=offset)
    return [RunResponse.from_row(r) for r in rows]
```

`min(limit, 200)` 这一行：**永远不要相信客户端传的分页大小**，服务端硬上限。一行代码防住 `?limit=999999999`。

`status: RunStatus | None` 是 `StrEnum`，FastAPI 会自动把查询字符串 `"queued"` 转成枚举，传个 `"xxx"` 直接 422。**校验免费**。

### 带状态转换的：POST

```python
# src/repopilot/api/routes.py:104
@router.post("/runs/{run_id}/cancel", response_model=RunResponse, status_code=202)
async def cancel_run(run_id: UUID) -> RunResponse:
    await _require(run_id)                                    # 不存在 → 404
    try:
        row = await runs_repo.transition(run_id, RunStatus.CANCELLED)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_row(row)
```

**注意这里的错误映射**：领域层抛的是 `InvalidTransition`（一个纯业务异常，不知道 HTTP 是什么），HTTP 层把它翻译成 **409 Conflict**。

为什么是 409 不是 400：400 = "你的请求格式不对"，409 = "请求没问题，但和当前资源状态冲突"。取消一个已经 published 的 run 属于后者。**这个区分能体现 API 设计功底。**

### 依赖注入

```python
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
```

`Depends(get_settings)` = "调用 `get_settings()`，把结果给我"。因为 `get_settings` 有 `@lru_cache`，实际上全进程共享一个实例。

自己写的依赖：

```python
# src/repopilot/api/routes.py:46
def get_bus(request: Request) -> EventBus:
    return request.app.state.bus

async def stream_events(run_id: UUID, bus: EventBus = Depends(get_bus)):
```

`app.state` 是 FastAPI 给你放全局对象的地方（≈ Spring 的 ApplicationContext）。`get_bus` 从里面把事件总线掏出来。

⚠️ ruff 的 `B008` 规则会说"别在默认值里调函数"，但 FastAPI 就是这么设计的，所以 `pyproject.toml` 里把它加进了 `ignore`。**知道为什么忽略一条 lint 规则，比不知道有这条规则强。**

## 2.3 `lifespan`：启动和停机

```python
# src/repopilot/api/app.py:23
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ---- yield 之前：启动 ----
    setup_logging()
    settings = get_settings()
    await init_pool(settings.database_url, min_size=..., max_size=...)
    app.state.bus = EventBus()
    if settings.enable_worker:
        worker = Worker(settings, app.state.bus)
        app.state.worker_task = asyncio.create_task(worker.run_forever(), name="worker")
    try:
        yield                        # ← 服务在这里对外提供服务，一直待在这
    finally:
        # ---- yield 之后：停机 ----
        if app.state.worker is not None:
            await app.state.worker.stop()
        if app.state.worker_task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(app.state.worker_task,
                                       timeout=settings.shutdown_grace_seconds + 5)
        await close_pool()
```

这是 [02-syntax.md](02-syntax.md) §14 那个 `@asynccontextmanager` 的最大用武之地：**启动和停机代码写在同一个函数里，配对关系一眼可见**。

Spring 里这是 `@PostConstruct` 和 `@PreDestroy` 两个分开的方法，你得自己记着谁配谁。

**顺序很讲究**：
- 启动：日志 → 连接池 → 事件总线 → worker → 接客
- 停机：**反过来**。先让 worker 停止领新活（`stop()`），等它把手上的干完（`wait_for`），最后才关连接池。
- 如果先关连接池，正在收尾的 worker 会因为拿不到连接而炸。

`timeout=shutdown_grace_seconds + 5`：给 worker 自己的宽限期（30s）再加 5 秒缓冲，因为 worker 内部还要处理取消。

`contextlib.suppress(CancelledError, TimeoutError)`：停机路径上再抛异常没有意义，压掉。

## 2.4 SSE：手写的流式响应

**SSE（Server-Sent Events）** = 服务器单向持续往客户端推数据的 HTTP 长连接。比 WebSocket 简单得多（单向、纯文本、走普通 HTTP）。

格式就是纯文本，每帧两个换行结尾：

```
event: node_completed
data: {"run_id":"...","seq":3,"type":"node_completed",...}

event: done
data: {}

```

代码：

```python
# src/repopilot/api/routes.py:222
@router.get("/runs/{run_id}/events")
async def stream_events(run_id: UUID, bus: EventBus = Depends(get_bus)):
    row = await _require(run_id)

    # 已经是终态：没有后续事件了，回放一次就关，别让客户端干等
    if row.status not in ACTIVE:
        queue = bus.subscribe(run_id, replay=True)
        queue.put_nowait(DONE)
    else:
        queue = bus.subscribe(run_id, replay=True)

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if not isinstance(item, RunEvent):        # 哨兵
                    yield "event: done\ndata: {}\n\n"
                    return
                yield f"event: {item.type}\ndata: {item.model_dump_json()}\n\n"
        except asyncio.CancelledError:
            raise                                          # 客户端断开
        finally:
            bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

四个设计点，每一个都值得说：

1. **终态检查**。如果 run 已经结束了，再订阅就永远等不到新事件 —— 客户端会挂在那里。所以先回放历史，再手动塞一个 `DONE` 哨兵让它立刻收尾。
2. **哨兵模式**。队列里流的是 `RunEvent`，但结束时塞一个 `DONE = object()`。用 `isinstance` 判断类型，不是 `RunEvent` 就是结束信号。比"塞一个特殊的 RunEvent"干净 —— **哨兵和数据在类型上就是两种东西，不可能混淆**。
3. **`finally` 里退订**。客户端断线时 FastAPI 会取消这个生成器，`finally` 保证队列从订阅列表里摘掉，不泄漏。
4. **`X-Accel-Buffering: no`**。告诉 Nginx 之类的反向代理别缓冲 —— 默认它们会攒一批再发，那流式就没了。**这一行是踩过坑的人才会写的。**

**Java 对照**：`SseEmitter` 或者 WebFlux 的 `Flux<ServerSentEvent>`。

试一下：
```bash
curl -N localhost:8000/runs/<id>/events      # -N 表示不缓冲
```

## 2.5 ASGI / uvicorn 是什么

- **WSGI** = 老的 Python Web 规范，同步的，一个请求占一个线程。Flask/Django 传统模式用它。
- **ASGI** = 新规范，异步的，一个线程上跑成千上万个连接。FastAPI 用它。
- **uvicorn** = 一个 ASGI 服务器，负责起事件循环、监听端口、把 HTTP 请求转成 ASGI 调用。

Java 对照：ASGI ≈ Servlet 规范，uvicorn ≈ Tomcat。FastAPI 是"框架"，uvicorn 是"容器"。

```bash
uv run uvicorn repopilot.api.app:app --reload --port 8000
#                └─ 模块路径 ────┘ └┬┘
#                                  └── 模块里那个叫 app 的变量
```

`--reload` = 改代码自动重启（开发用，生产别开）。

所以 `app.py` 最后那行是必须的：

```python
app = create_app()      # uvicorn 要 import 到一个叫 app 的对象
```

## 2.6 自动文档

跑起来后开 **http://localhost:8000/docs** —— Swagger UI，所有接口能直接在网页上点着试。

它是从**类型注解 + Pydantic 模型 + Field 的 description** 自动生成的，你没有写任何文档。`/openapi.json` 是原始的 OpenAPI 规范。

---

# §3 PostgreSQL + asyncpg：数据库连接完全指南

## 3.1 数据库跑在哪：Docker

```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:0.8.6-pg17-trixie
    container_name: repopilot-pg
    environment:
      POSTGRES_USER: repopilot
      POSTGRES_PASSWORD: repopilot
      POSTGRES_DB: repopilot
    ports: ["5433:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
```

逐行：

- `image`：用的是 **pgvector 镜像**（里面是 PostgreSQL 17 + 向量扩展）。选它是因为**本机已经有这个镜像了**，Docker Hub 当时拉不动。附带好处：将来想做语义代码搜索不用换镜像。
- `ports: ["5433:5432"]`：**宿主机 5433 → 容器内 5432**。用 5433 是为了不和你本机可能已有的 PG 撞车。**所以连接串里写 5433。**
- `volumes` 第一条：数据目录挂到一个命名卷，**容器删了数据还在**。
- `volumes` 第二条：把 `db/schema.sql` 挂进 `/docker-entrypoint-initdb.d/`。这个目录是 PG 官方镜像的约定 —— **数据目录为空时（也就是第一次启动）**，会自动按文件名顺序执行里面所有 `.sql`。
  - ⚠️ **只在第一次生效**。改了 `schema.sql` 之后必须 `make db-reset`（它做的就是 `docker compose down -v` 删掉卷再起）。

`make db-up` 还多做一件事 —— **等到数据库真的能连了才返回**：

```makefile
db-up:
	docker compose up -d
	@until docker exec repopilot-pg pg_isready -U repopilot -d repopilot >/dev/null 2>&1; \
	  do echo "等 postgres 起来…"; sleep 1; done
```

容器"启动了"不等于"能连了"，PG 还要初始化几秒。没有这个等待循环，`make test` 会随机失败。

## 3.2 连接串（DSN）怎么读

```
postgresql://repopilot:repopilot@localhost:5433/repopilot
└───┬────┘   └───┬───┘ └───┬───┘ └───┬───┘ └┬─┘ └───┬───┘
  协议         用户名     密码      主机     端口   数据库名
```

Java 对照：`jdbc:postgresql://localhost:5433/repopilot`，用户名密码单独配。Python 这边习惯全塞进一个 URL。

配置在 `config.py:40`，可以用环境变量 `REPOPILOT_DATABASE_URL` 覆盖。

## 3.3 GUI 工具怎么连

推荐 **DBeaver**（免费、跨平台、Mac ARM 原生）。新建 PostgreSQL 连接，填：

| 字段 | 值 |
|---|---|
| Host | `localhost` |
| Port | **5433** |
| Database | `repopilot` |
| Username | `repopilot` |
| Password | `repopilot` |

或者直接用命令行：`make psql`（等价于 `docker exec -it repopilot-pg psql -U repopilot -d repopilot`）。

psql 里常用的：
```sql
\dt                        -- 列出所有表（= MySQL 的 show tables）
\d runs                    -- 看表结构（= desc runs）
\dT+ run_status            -- 看枚举类型有哪些值
\x                         -- 开关"竖排显示"，字段多的时候救命
SELECT id, status, task FROM runs ORDER BY created_at DESC LIMIT 5;
```

## 3.4 asyncpg：不是 JDBC，更不是 ORM

**本项目故意不用 ORM（SQLAlchemy），直接写裸 SQL。** 两个理由：

1. 你正在从 MySQL 转 PostgreSQL，**裸 SQL 才能真的学到 PG 的东西**。
2. 核心那条 `FOR UPDATE SKIP LOCKED` 的领取语句，在 ORM 里写出来又丑又难懂，反而遮住了重点。

代价：加字段要改两处（`schema.sql` 和 `db/models.py`）。MVP 阶段可以接受，`db/models.py` 的 docstring 里写明了这个取舍。

### 连接池

```python
# src/repopilot/db/pool.py:17
_pool: asyncpg.Pool | None = None      # 模块级变量 = 进程内单例

async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            init=_register_codecs,
        )
    return _pool
```

**Java 对照：HikariCP。** 一模一样的"借出-归还"模型，区别是这里借出的连接绑在协程上，不占线程。

`global _pool` 是 Python 里改模块级变量的写法（不写 `global` 的话 `_pool = ...` 会创建一个局部变量）。**模块级变量 + 函数 = 最轻量的单例**，不需要任何容器。

`command_timeout=30`：单条 SQL 最多跑 30 秒。防止一条烂查询把连接永久占住。

### `init=_register_codecs`：让 jsonb 变成 dict

```python
async def _register_codecs(conn: asyncpg.Connection) -> None:
    import json
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
        )
```

**默认 asyncpg 把 jsonb 当字符串给你**，于是你要在每个读到 jsonb 的地方写 `json.loads`。这里注册一次编解码器，之后：

- 写：传 Python 的 `dict` 进去，自动 `json.dumps`
- 读：出来直接就是 `dict`

`init=` 参数的意思是"每条新连接建立时都跑一遍这个函数"，因为编解码器是连接级别的设置。

Java 对照：≈ 注册一个 `TypeHandler`（MyBatis）或者 `AttributeConverter`（JPA）。

### 事务

```python
# src/repopilot/db/pool.py:62
@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        yield conn
```

用起来：

```python
async with transaction() as conn:
    await conn.execute(...)
    await conn.fetchrow(...)
# 正常退出 → 自动 COMMIT
# 抛异常   → 自动 ROLLBACK，异常继续往上抛
```

一行 `async with pool.acquire() as conn, conn.transaction():` 里有两个上下文管理器，**从左到右进入，从右到左退出**：借连接 → 开事务 →（你的代码）→ 提交/回滚 → 归还连接。

**和 `@Transactional` 的差别**：Spring 的事务边界是注解，看不见，还有"自调用不生效"之类的坑。这里边界是一个 `with` 块，**缩进就是事务范围**，一眼看到头。

### 四个执行方法

| 方法 | 返回 | 什么时候用 |
|---|---|---|
| `fetchrow(sql, *args)` | 一行 `Record` 或 `None` | 查/改单行，配 `RETURNING *` |
| `fetch(sql, *args)` | `list[Record]` | 查多行 |
| `fetchval(sql, *args)` | 单个值 | 查 count 之类 |
| `execute(sql, *args)` | 状态字符串 `"UPDATE 1"` | 不需要返回数据 |

`execute` 返回的那个字符串是本项目两个关键判断的依据（见 [02-syntax.md](02-syntax.md) §5）。

### `$1` 占位符

```python
await conn.fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
```

**PostgreSQL 用 `$1 $2 $3`，MySQL/JDBC 用 `?`。**

好处：**同一个参数可以在 SQL 里重复引用**（`$1` 写两遍就行），`?` 就得传两次。

⚠️ **永远不要用 f-string 拼 SQL 的值**，那是 SQL 注入。本项目唯一拼字符串的地方是 `transition()` 里的**列名**（SQL 语法不允许列名参数化），而且列名全来自我们自己的代码。这个边界要能说清楚。

### `Record` → Pydantic

```python
# src/repopilot/db/models.py:43
@classmethod
def from_record(cls, record: Any) -> "RunRow":
    """asyncpg.Record 长得像 dict，直接喂给 Pydantic 校验。"""
    return cls.model_validate(dict(record))
```

`Record` 是 asyncpg 的行对象，支持 `record["column"]` 和 `dict(record)`。转成 dict 后 Pydantic 按字段名对应过去，顺便把 `timestamptz` 转成 `datetime`、把文本 `'queued'` 转成 `RunStatus.QUEUED`。

**手写的 ORM 映射，30 行搞定。**

## 3.5 PostgreSQL 相对 MySQL 的六个新东西

这一节直接对着 `db/schema.sql` 讲，是你从 MySQL 转过来最该记的。

### (1) `ENUM` 是独立的类型

```sql
CREATE TYPE run_status AS ENUM ('queued', 'running', 'pending_approval', ...);
CREATE TABLE runs (status run_status NOT NULL DEFAULT 'queued', ...);
```

MySQL：`status ENUM('a','b')` 写在列上，每个表各写各的。
PG：**类型是独立对象，可以被多个表复用**。加值要 `ALTER TYPE run_status ADD VALUE 'x'`。

副作用：写 SQL 时经常要显式转型 `$2::run_status`，因为参数传过来是 text。本项目到处能看到这个 `::`。

### (2) `jsonb`

```sql
evaluation  jsonb,
step_log    jsonb NOT NULL DEFAULT '[]'::jsonb,
```

`jsonb` = **二进制存储的 JSON**，可以建索引、可以用 `->` `->>` `@>` 直接查内部字段：

```sql
SELECT * FROM runs WHERE evaluation->>'failure_reason' = 'tests_failed';
SELECT * FROM runs WHERE evaluation @> '{"task_success": true}';
```

MySQL 的 JSON 类型能力弱很多（没有 GIN 索引、操作符少）。

### (3) 原生数组

```sql
files_changed text[] NOT NULL DEFAULT '{}',
```

MySQL 没有数组类型，只能存 JSON 或者另开一张关联表。PG 有真数组，asyncpg 直接映射成 Python 的 `list[str]`。

### (4) ⭐ 部分索引（partial index）—— 本次最该记的

```sql
CREATE INDEX idx_runs_claimable
    ON runs (created_at)
    WHERE status IN ('queued', 'running');
```

**只给"待领取"的行建索引。**

为什么重要：队列表跑一年之后，99% 的行是 `published` / `failed` 的历史数据。全量索引又大又没用 —— 每次领取任务都要扫过一堆已完成的行。部分索引只索引那 1%，**索引体积小一个数量级，查询快，写入时维护成本也低**（更新已完成的行根本不碰这个索引）。

**MySQL 完全没有这个功能。** 这是 PG 一个真正的降维打击。

另一个：

```sql
CREATE INDEX idx_runs_external_ref ON runs (external_ref) WHERE external_ref IS NOT NULL;
```
只有 GitHub 触发的 run 才有 `external_ref`，手动触发的全是 NULL —— 没必要索引一堆 NULL。

### (5) `RETURNING`

```sql
UPDATE runs SET status = 'running', ... WHERE id = (...) RETURNING *
INSERT INTO webhook_deliveries (...) VALUES (...) ON CONFLICT DO NOTHING RETURNING delivery_id
```

**改完直接把行返回来，不用再查一次。**

MySQL 必须 `UPDATE` 然后再 `SELECT`，而这中间有并发窗口 —— 你更新的那行可能已经被别人改了，第二次查到的不是你改的那个状态。

**这才是 PG 在队列场景的真正优势**（见下一条）。

### (6) `FOR UPDATE SKIP LOCKED`

```sql
SELECT id FROM runs WHERE ... ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
```

| 写法 | 10 个 worker 同时抢 |
|---|---|
| 什么都不加 | 全读到同一行，**同一个任务跑 10 次** |
| `FOR UPDATE` | 排队等锁，退化成串行，吞吐崩了 |
| `FOR UPDATE SKIP LOCKED` | 跳过被别人锁住的行，各拿各的，**互不阻塞** |

⚠️ **面试千万别说错**：`SKIP LOCKED` **MySQL 8.0 也有**，不是 PG 独有的。说错这个会很尴尬。

PG 在这个场景的真正优势是 **`RETURNING`**（领取和取数据一条语句完成）和**部分索引**。

### 什么时候不该用数据库当队列

要能主动讲这个取舍，否则显得没想过：

> 该换 MQ 的信号：吞吐到万级 TPS、需要扇出/多消费组、需要跨服务解耦。
> 我这里用表是因为：任务是分钟级的、量小，而且**任务本身就是业务实体** —— 要查询、要审批、要展示历史。用表的话，入队和业务写入天然在同一个事务里，**不存在 MQ 和 DB 双写不一致**的问题。

---

# §4 LangGraph

## 4.1 它是什么

一个**把 Agent 流程画成有向图**的库。节点是函数，边是流转规则。

```
START → analyze → plan → execute → run_tests → evaluate ─┬→ finish → END
                           ↑                             │
                           └────────── retry ────────────┘
```

## 4.2 三个概念

### State（状态）

```python
# src/repopilot/agent/state.py:17
class AgentState(TypedDict, total=False):
    run_id: str
    task: str
    analysis: Analysis | None
    ...
    tool_calls: Annotated[list[ToolCallRecord], operator.add]
    errors: Annotated[list[str], operator.add]
    step_log: Annotated[list[str], operator.add]
    retry_count: int
    max_retries: int
```

就是一个字典，从 START 一路传到 END，每个节点往里加东西。

### Node（节点）

一个函数，**输入是完整 state，输出是"要改的那部分"**：

```python
async def analyze(self, state: AgentState) -> dict[str, Any]:
    ...
    return {
        "analysis": analysis,
        "tool_calls": [_record(listing, glob="**/*.py")],
        "step_log": [f"analyze: {len(analysis.relevant_files)} candidate files"],
    }
```

**铁律：不要改传进来的 state，返回一个新字典。** 框架负责合并。

Java 对照：这是**不可变数据 + 归约**，类似 Redux/事件溯源，不是 Spring 那种"注入 Service 然后到处 set"。

### Reducer（合并规则）

这是 LangGraph 最值得讲的设计。

- **没有 `Annotated` 的键**：新值**覆盖**旧值。`{"verdict": "success"}` → state 里的 verdict 变成 success。
- **有 `Annotated[list, operator.add]` 的键**：新值和旧值**相加**（列表就是拼接）。

所以 `execute` 节点第 1 次返回 `{"step_log": ["execute attempt 1: ..."]}`，第 2 次返回 `{"step_log": ["execute attempt 2: ..."]}`，最终 state 里是**两条都在**的列表。

**没有 reducer 的话**，每个节点都得写：
```python
old = state.get("step_log") or []
return {"step_log": old + ["新的一条"]}      # 读-改-写，啰嗦且容易忘
```

**面试口径**：
> "节点返回的是**增量**不是全量，合并语义**按 key 声明**在类型上。累积型的字段（工具调用记录、错误、步骤日志）用 `operator.add` 拼接，跨三次重试自动累加；其余字段覆盖。这样节点不需要读-改-写，也不会互相覆盖。"

## 4.3 建图

```python
# src/repopilot/agent/graph.py:32
graph = StateGraph(AgentState)
graph.add_node("analyze", nodes.analyze)
...
graph.add_edge(START, "analyze")
graph.add_edge("analyze", "plan")
graph.add_edge("plan", "execute")
graph.add_edge("execute", "run_tests")
graph.add_edge("run_tests", "evaluate")
graph.add_conditional_edges(
    "evaluate",
    route_after_evaluate,                     # 一个函数：state → 下一个节点的名字
    {"execute": "execute", "finish": "finish"},   # 名字 → 真实节点
)
graph.add_edge("finish", END)
return graph.compile()
```

条件边的路由函数：

```python
def route_after_evaluate(state: AgentState) -> str:
    """Conditional edge: a pure function of state -> next node name."""
    return "execute" if state.get("verdict") == "retry" else "finish"
```

**注意它是纯函数**：只读 state，不做任何计算和判断逻辑。判断在 `evaluate` 节点里已经做完并写进 `verdict` 了，路由只是**读结果**。

好处：路由能被单独测试，不用起整个图。`tests/test_graph.py::test_conditional_edge_routes_on_verdict` 就是直接把它当普通函数调。

## 4.4 为什么用图不用 while 循环

诚实版本（面试要这么说，不要吹）：

> **好处**：重试路径、预算检查、退出条件是**声明成边的**，也就是数据 —— 可检视、可流式输出、可单独测试。`while` 循环把同样的逻辑埋在控制流里，只能整体跑一遍才能测。另外流式输出（`astream`）和检查点是白送的。
>
> **诚实的取舍**：就这个"六节点线性 + 一条重试边"的流程而言，`while` 循环也能写，而且更短。图的收益在于**加分支的时候** —— 比如加人工介入节点、加多个执行器。我选它也是为了学这个范式。

面试官最喜欢这种"知道自己在用杀鸡的牛刀，并且说得出为什么"的回答。

## 4.5 流式执行

```python
# src/repopilot/worker/runner.py:73
async for chunk in graph.astream(state, stream_mode="updates"):
    for node_name, partial in chunk.items():
        state = {**state, **_merge(state, partial)}
        emit("node_completed", node=node_name, ...)
```

`stream_mode="updates"` = **每个节点跑完就吐一次它的增量**（而不是等全图跑完）。

`chunk` 长这样：`{"analyze": {"analysis": ..., "step_log": [...]}}` —— 键是节点名，值是那个节点返回的部分 state。

`async for` = 异步迭代，配合异步生成器用（见 [02-syntax.md](02-syntax.md) §18）。

`{**state, **_merge(state, partial)}` 是**字典合并语法**：把两个字典摊平成一个新字典，后面的覆盖前面的。

⚠️ 为什么 Runner 要自己维护一份 state 副本、还要自己实现 `_merge`：

```python
def _merge(state: dict, partial: dict) -> dict:
    """镜像 LangGraph 的 reducer，让我们手上这份 state 副本保持准确。"""
    merged = {}
    for key, value in partial.items():
        if key in ("tool_calls", "errors", "step_log"):
            merged[key] = (state.get(key) or []) + list(value or [])   # 累加
        else:
            merged[key] = value                                         # 覆盖
    return merged
```

因为 `astream` 只给增量，**框架内部的完整 state 我们拿不到**。Runner 需要完整 state 来做评估和落库，所以在外面**镜像一份 reducer 逻辑**。

这是个真实的耦合点，面试可以主动提：
> "这里有个已知的重复 —— reducer 的语义在 `state.py` 的 `Annotated` 里声明了一遍，在 `runner._merge` 里又实现了一遍。两边不同步就会出 bug。更好的做法是用 `stream_mode="values"` 直接拿全量 state，或者跑完之后用 `graph.ainvoke` 的返回值。这是我知道但暂时没改的技术债。"

---

下一章：[05-happy-path.md](05-happy-path.md) —— 现在把上面所有东西串起来，走一遍完整的主线。
