# 02 · 高频语法大全（按本项目实际用到的）

**这一章当字典用，不要通读。** 主线读到不认识的写法，回来查对应小节。

每一节的格式固定：**长什么样 → 干什么 → 本项目哪里用了 → Java 对照 → 坑**。

---

## §1 缩进就是花括号

Python 没有 `{}`，用**缩进**表示代码块。冒号 `:` 后面必须换行缩进。

```python
if x > 0:
    print("正")        # 缩进 4 空格 = 进了 if 的块
    print("还在块里")
print("出块了")        # 回到左边 = 出了 if
```

- **统一 4 个空格**，不要用 Tab（`.vscode/settings.json` 已经配好自动转空格）。
- 缩进错了是**语法错误**，不是风格问题。这是 Python 最大的适应成本，但两天就习惯了。

**空块占位用 `pass`**（相当于 Java 的 `{}`）：

```python
class LLMError(RuntimeError):
    pass                        # src/repopilot/llm/base.py:23
```

`...`（三个点，叫 `Ellipsis`）也能占位，惯例上用在**协议/接口声明**里表示"这里没有实现"：

```python
# src/repopilot/llm/base.py:20
async def structured(self, *, system: str, user: str, schema: type[T]) -> T: ...
```

---

## §2 注释和 docstring

```python
# 单行注释，井号

"""三个引号是多行字符串。
写在函数/类/模块的第一行时，它就叫 docstring，是这个东西的官方说明。"""
```

**docstring 不只是注释，它是一个真实的属性** `__doc__`，能在运行时读出来。本项目真的用到了：

```python
# src/repopilot/llm/anthropic_client.py:41
"description": schema.__doc__ or f"Return a {schema.__name__}",
```

这里把 Pydantic 模型的 docstring 当成**发给大模型的工具描述**。也就是说 —— **你给 `Analysis` 类写的那句注释，会真的被发到模型那边影响它的输出**。Java 里 Javadoc 编译完就没了，Python 里它一直在。

---

## §3 类型注解：写了但不强制

```python
def heartbeat(run_id: UUID, worker_id: str, lease_seconds: int = 120) -> bool:
```

- `run_id: UUID` = 参数类型
- `-> bool` = 返回类型
- `lease_seconds: int = 120` = 有默认值

**最重要的一句话：Python 运行时完全不检查这些注解。** 你传个字符串进去，它照跑不误，直到某行代码真的用不了才炸。

那为什么还写？
1. **IDE 靠它做补全和跳转**。不写注解，VSCode 就是个记事本。
2. **给人读**。函数签名就是文档。
3. Pydantic 是例外 —— 它**会**在运行时读注解并真的校验（见 [04-stack.md](04-stack.md) §1）。

Java 对照：Java 的类型是编译期强制的；Python 的注解更像是「加强版的 Javadoc + IDE 提示」。

### 常用注解写法速查

| 写法 | 意思 | Java 对照 |
|---|---|---|
| `list[str]` | 字符串列表 | `List<String>` |
| `dict[str, int]` | 键是 str 值是 int | `Map<String,Integer>` |
| `str \| None` | 要么字符串要么 None | `@Nullable String` |
| `RunRow \| None` | 可能没有 | `Optional<RunRow>` |
| `list[Path]` | | `List<Path>` |
| `dict[UUID, list[asyncio.Queue]]` | 嵌套 | `Map<UUID, List<Queue>>` |
| `type[T]` | **类本身**，不是实例 | `Class<T>` |
| `Any` | 放弃治疗，什么都行 | `Object` |

`str | None` 这个竖线是 Python 3.10+ 的写法，老代码里会看到 `Optional[str]`，一回事。

本项目实例：

```python
# src/repopilot/db/runs.py:186
async def get_run(run_id: UUID) -> RunRow | None:      # 查得到返回行，查不到返回 None
```

---

## §4 变量、None、真假判断

```python
x = 5                    # 不用声明类型，不用 var
name: str = "hi"         # 也可以写注解，同样不强制
```

**`None` = Java 的 `null`。** 判断用 `is None` / `is not None`，**不要用 `== None`**。

```python
if record is None:       # src/repopilot/db/deliveries.py:60 的思路
    return None
```

为什么用 `is`：`is` 比较「是不是同一个对象」，`==` 会去调用对象的 `__eq__` 方法，可能被重写成任何行为。`None` 全进程只有一个实例，所以 `is None` 又快又准。

**「假值」的范围比 Java 大得多。** 下面这些在 `if` 里全部算 False：

```python
False, None, 0, 0.0, "", [], {}, set(), ()
```

所以本项目里到处是这种写法：

```python
if not matches:                    # 列表空 → 进这里
if not self._inflight:             # src/repopilot/worker/worker.py:94
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)   # nxt 是空集合 → 是终态
```

最后这行很典型：**`if not nxt` 就是「这个状态没有任何出边」**，等价于 Java 的 `nxt.isEmpty()`。

**⚠️ 这也是坑**：`if x:` 和 `if x is not None:` 不一样。`x = 0` 或 `x = ""` 时前者是 False。所以判断「有没有传参」一定要用 `is None`。

---

## §5 字符串

```python
s = "双引号"
s = '单引号'                  # 完全等价，本项目统一用双引号（ruff 会管）
s = """多行
字符串"""
```

### f-string（格式化，最常用）

```python
f"worker={worker_id} 领取 run={row.id}"
f"{i:>4} | {line}"              # >4 = 右对齐宽度4，src/repopilot/tools/fs_tools.py:42
f"{rel}:{lineno}: {line.strip()}"
f"path escapes workspace: {relative_path!r}"    # !r = 用 repr()，字符串会带引号，方便看清空格
```

Java 对照：`String.format()` / 文本块，但 f-string 是把表达式直接嵌进去，更短。

### `.format()`（本项目在提示词里用）

```python
# src/repopilot/agent/nodes.py:53
prompts.ANALYZE_USER.format(task=state["task"], tree=listing.content)
```

模板字符串先定义好（在 `prompts.py`），到用的时候再填坑。为什么这里不用 f-string？因为**模板要提前定义、稍后填充**，f-string 是定义时就求值的。

### 常用方法

```python
text.splitlines()              # 按行切成列表
line.strip()                   # 去掉首尾空白
"\n".join(paths)               # 列表拼成字符串，用换行连接 ← 极高频
s.startswith("x") / s.endswith(" 1")
s.rsplit(" ", 1)[-1]           # 从右边切一刀，取最后一段
s.partition(" | ")             # 切成 (前, 分隔符, 后) 三段
text[-3000:]                   # 取最后 3000 个字符（切片，见 §7）
```

本项目里两个精妙的用法：

```python
# src/repopilot/db/runs.py:114
return result.endswith(" 1")
```
asyncpg 的 `execute()` 返回一个状态字符串，比如 `"UPDATE 1"` 或 `"UPDATE 0"`。所以「更新到了恰好 1 行」= 字符串以 `" 1"` 结尾。**这是续租成功与否的判定**。

```python
# src/repopilot/db/runs.py:129
count = int(result.rsplit(" ", 1)[-1])
```
`"UPDATE 7"` → 从右切一刀 → `["UPDATE", "7"]` → 取最后一个 → `"7"` → 转 int。**这是回收了几个僵尸任务**。

---

## §6 四种容器

| 类型 | 字面量 | 特点 | Java |
|---|---|---|---|
| `list` 列表 | `[1, 2, 3]` | 有序、可变、可重复 | `ArrayList` |
| `tuple` 元组 | `(1, 2, 3)` | 有序、**不可变** | 不可变的定长记录 |
| `dict` 字典 | `{"a": 1}` | 键值对，**保证插入顺序** | `LinkedHashMap` |
| `set` 集合 | `{1, 2, 3}` | 去重、无序 | `HashSet` |
| `frozenset` | `frozenset({1,2})` | 不可变集合 | `Set.of()` |

```python
paths = []                     # 空列表
matches: list[str] = []        # 带注解的空列表（推荐，IDE 才知道里面装什么）
d = {}                         # 空字典
s = set()                      # 空集合，注意 {} 是空字典不是空集合！
```

### 常用操作

```python
lst.append(x)                  # 加一个
lst[0] / lst[-1]               # 第一个 / 最后一个（负数从后数！）
len(lst)
x in lst                       # 包含判断
del buffer[0]                  # 删第一个，src/repopilot/worker/bus.py:31

d["key"]                       # 取值，没有会抛 KeyError
d.get("key")                   # 取值，没有返回 None ← 更安全
d.get("key", 默认值)            # 没有就返回默认值 ← 高频
d.items()                      # 遍历键值对
d.pop("key", None)             # 删掉并返回，没有就返回 None

set_a - set_b                  # 差集，ACTIVE = frozenset(TRANSITIONS) - TERMINAL
```

`.get()` 带默认值在本项目里到处都是，因为 `AgentState` 是个可能缺键的字典：

```python
state.get("verdict")                       # 可能还没算出来 → None
state.get("files_changed") or []           # 没有或是 None → 给个空列表
state.get("retry_count", 0)                # 没有就当 0
TRANSITIONS.get(current, frozenset())      # 脏数据 → 空集合 → 自然不允许任何流转
```

最后这个很值得记：**用 `.get` 的默认值把「未知输入」优雅地降级成「最保守的行为」**，比写 `if current not in TRANSITIONS: raise` 干净。

### 解包（unpacking）

```python
head, sep, tail = line.partition(" | ")     # 一次拆三个变量
stdout, stderr = await proc.communicate()   # 函数返回元组，直接拆开
```

Java 没有这个语法，得写三行 `.get(0)`。

`*` 展开：

```python
await asyncio.gather(*read_jobs, *search_jobs)      # 把两个列表摊平成一堆参数
subprocess.run([*command])
registry.register(fn.__tool_spec__)
```

`*read_jobs` = 把列表里每个元素当成独立参数传进去。Java 对照：`toArray()` 后传可变参数。

---

## §7 切片

```python
lst[a:b]     # 从 a 到 b（不含 b）
lst[:5]      # 前 5 个
lst[-1]      # 最后一个
lst[2:]      # 从第 3 个到结尾
text[-3000:] # 最后 3000 个字符
```

**切片永远不会越界报错**，不够就给你剩下的。这是 Python 和 Java 一个巨大的体验差异。

本项目高频用法：**给大模型的输入做截断**。

```python
analysis.relevant_files[:5]        # 最多读 5 个文件，模型说 40 个也只取 5 个
analysis.search_queries[:3]        # 最多搜 3 次
plan.files_to_edit[:5]
edit_set.edits[:5]
last.output[-3000:]                # 测试输出只回传最后 3000 字符（错误信息在末尾）
json.dumps(args, default=str)[:120]
lines[-1][:200]
```

**这不是随手写的，这是预算控制。** 模型可能返回任意长的列表，切片是最后一道闸门 —— 就算提示词里说了"最多 5 个"，代码层面也不能信。

面试口径：**"不信任模型输出"不是一句口号，落到代码上就是这些切片和 `.get()` 默认值。**

---

## §8 推导式（comprehension）

一行写完「遍历 + 过滤 + 变换」。**这是本项目最密集的语法**，必须看懂。

```python
[表达式 for 变量 in 可迭代 if 条件]
```

翻译成 Java 就是 Stream：

```python
paths = [workspace.relative(p) for p in workspace.iter_files(glob)]
```
≡ `files.stream().map(ws::relative).collect(toList())`

```python
records = [_record(r) for r in results]
lines = [line for line in text.strip().splitlines() if line.strip()]
```
≡ `.filter(l -> !l.isBlank()).collect(toList())`

```python
return [RunRow.from_record(r) for r in records]
changed = [e.path for e, w in zip(edit_set.edits, writes, strict=False) if w.ok]
```

字典推导式：

```python
return {r["s"]: r["n"] for r in records}                        # db/runs.py:224
counts = {kind: int(n) for n, kind in _SUMMARY.findall(output)}  # tools/test_runner.py:43
```

集合推导式（本项目最漂亮的一行）：

```python
# src/repopilot/domain/status.py:55
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
```
读法：「遍历流转表的每一对（状态, 能去的状态集合），挑出**能去的集合是空的**那些状态，装成不可变集合」= **终态从表推导出来，不手写第二份清单**。

生成器表达式（没有方括号，惰性求值，省内存）：

```python
"\n\n".join(f"### {...}\n{r.content}" for r in results if r.ok)   # nodes.py:79
if any(part in _IGNORED for part in p.parts): continue            # workspace/manager.py:48
```

`any(...)` = 「有任意一个满足就 True」≡ Java `anyMatch`。`all(...)` 是 `allMatch`。

---

## §9 循环

```python
for x in lst:                    # 直接遍历元素，没有 for(int i=0;...)
for i, line in enumerate(text.splitlines(), start=1):    # 要下标就用 enumerate
for k, v in d.items():           # 遍历字典
for a, b in zip(list1, list2, strict=False):             # 并排遍历两个列表
while not self._stopping.is_set():                       # while 照旧
```

`enumerate(x, start=1)` = 边遍历边给序号，从 1 开始。行号计数全靠它：

```python
# src/repopilot/tools/fs_tools.py:113
for lineno, line in enumerate(text.splitlines(), start=1):
```

`zip(a, b, strict=False)` = 把两个列表配对。`strict=False` 表示长度不一样时按短的来（不报错）。本项目：

```python
# src/repopilot/agent/nodes.py:157
changed = [e.path for e, w in zip(edit_set.edits, writes, strict=False) if w.ok]
```
把「模型要求的编辑」和「实际写入的结果」配对，挑出真的写成功的。

`break` / `continue` 和 Java 一样。`search_code` 里两层循环 + 两个 `break` 就是标准用法。

---

## §10 函数

```python
def add(a, b):                       # 普通
    return a + b

def create(source, run_id=None):     # 默认值
    ...

def f(*args, **kwargs):              # 收集所有位置参数 / 所有关键字参数
    ...
```

### `*` 和 `/` 这两个奇怪的参数

```python
# src/repopilot/db/runs.py:22
async def create_run(
    *,                                # ← 这个星号是分隔符
    task: str,
    repo_path: str,
    source: str = "manual",
    ...
) -> RunRow:
```

**裸 `*` 表示：后面所有参数必须用关键字传。**

```python
create_run(task="修 bug", repo_path="/x")   # ✅
create_run("修 bug", "/x")                   # ❌ TypeError
```

为什么这么写？因为这个函数有 6 个参数、其中好几个是字符串。位置传参写出来是 `create_run(a, b, c, d, e)` —— 谁也看不出哪个是哪个，而且**调换两个字符串参数编译器不会报错**。强制关键字让调用点自解释。

Java 对照：Java 没有这个能力，所以才要写 Builder 模式。**Python 的 `*` 是零成本的 Builder。**

反过来，斜杠 `/` 表示前面的必须**位置**传：

```python
# src/repopilot/tools/base.py:36
async def __call__(self, workspace: Workspace, /, **kwargs: Any) -> ToolResult: ...
```
意思是：第一个参数一定是 workspace，位置传；其余随便。

### `**kwargs`

```python
# src/repopilot/db/runs.py:136
async def transition(run_id, target, *, expected=None, **fields: Any) -> RunRow:
```

`**fields` 把所有额外的关键字参数收成一个字典。调用方：

```python
await runs_repo.transition(row.id, RunStatus.PENDING_APPROVAL,
                           verdict=..., diff=..., evaluation=...)
```
→ 函数里 `fields == {"verdict": ..., "diff": ..., "evaluation": ...}`

然后拿这个字典**动态拼 SQL 的 SET 子句**：

```python
for i, (column, value) in enumerate(fields.items(), start=3):
    sets.append(f"{column} = ${i}")
    args.append(value)
```

一个 `transition` 函数就能更新任意字段组合，不用写十个方法。Java 要做到得靠反射或者 MyBatis 的动态 SQL。

⚠️ **注意这里的安全边界**：列名 `column` 是拼进 SQL 字符串的（因为 SQL 不允许把列名参数化），**值 `value` 是用 `$N` 占位符传的**。列名全部来自我们自己的代码，不来自用户输入，所以安全。这个区别面试可能会问。

### 函数是值

```python
ALL_TOOLS = [list_files, read_file, ...]       # 函数进列表
graph.add_node("analyze", nodes.analyze)        # 方法当参数传
graph.add_conditional_edges("evaluate", route_after_evaluate, {...})
key=lambda x: x.created_at                      # lambda = 匿名函数
```

---

## §11 类

```python
class Worker:
    def __init__(self, settings, bus, worker_id=None):   # 构造器
        self.settings = settings                          # 字段就是这么"声明"的
        self._slots = asyncio.Semaphore(...)              # 下划线开头 = 约定的 private

    async def run_forever(self) -> None:                  # 方法，第一个参数必须是 self
        ...
```

三个和 Java 不同的点：

1. **`self` 必须显式写出来**，相当于 Java 的 `this`，但 Java 帮你隐藏了。调用时不用传：`worker.run_forever()`。
2. **字段不预先声明**，在 `__init__` 里 `self.x = ...` 就算有了。
3. **没有 `private` 关键字**。约定：`_name` 表示"内部用的，别碰"，纯君子协定。`__name`（两个下划线）会触发名字改写，但很少用。

### `@dataclass`

```python
# src/repopilot/workspace/manager.py:24
@dataclass(slots=True)
class Workspace:
    run_id: str
    root: Path

    def resolve(self, relative_path: str) -> Path:
        ...
```

`@dataclass` 自动生成 `__init__`、`__repr__`、`__eq__`。等于 Java 的 **Lombok `@Data`** 或者 **record**。

`slots=True` 是性能优化：告诉 Python "这个类的字段就这两个，别给我建那个可以随便加属性的字典"。省内存、访问快一点，代价是不能动态加属性。

### `@staticmethod` / `@classmethod`

```python
@staticmethod
def _git_init(ws: Workspace) -> None:      # 不需要 self，就是个挂在类里的普通函数

@classmethod
def from_record(cls, record: Any) -> "RunRow":     # 第一个参数是类本身
    return cls.model_validate(dict(record))
```

`@classmethod` 是 Python 版的**静态工厂方法**。`cls` 就是这个类，`cls(...)` 等于 `RunRow(...)`。好处是子类继承时 `cls` 会自动变成子类。

返回类型写成字符串 `"RunRow"` 是因为**类还没定义完，名字还不存在**。这叫前向引用。

### `@property`

```python
# src/repopilot/sandbox/local.py:28
@property
def ok(self) -> bool:
    return self.exit_code == 0 and not self.timed_out
```

让方法**像字段一样访问**：`result.ok` 而不是 `result.ok()`。Java 对照：一个没有对应字段的 getter。

---

## §12 枚举 / `StrEnum`

```python
# src/repopilot/domain/status.py:16
class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ...
```

`StrEnum` = **既是枚举，又是字符串**。所以下面全都成立：

```python
RunStatus.QUEUED == "queued"          # True ← 普通 Enum 做不到
f"{RunStatus.QUEUED}"                 # "queued"
RunStatus("queued")                   # 反查，从字符串得到枚举
RunStatus.QUEUED.value                # "queued"
```

**为什么这个特性对本项目至关重要**：数据库里存的是文本 `'queued'`，Python 里是枚举。有了 `StrEnum`，两边可以直接比较、直接传，不用到处 `.value` 和 `RunStatus(...)` 来回转。

（不过写 SQL 参数时本项目还是显式写了 `.value`，见 `db/runs.py:166` —— 因为 asyncpg 传参时要的是干净的 `str`，显式转换更稳。）

`sorted(TRANSITIONS.get(current, frozenset()))` 能直接排序，也是因为它是 str。

---

## §13 异常

```python
try:
    regex = re.compile(pattern)
except re.error as exc:                      # 只抓这一种
    return ToolResult(ok=False, error=f"invalid regex {pattern!r}: {exc}")
except (UnicodeDecodeError, OSError):        # 一次抓多种
    continue
except Exception as exc:                     # 兜底，抓一切
    ...
finally:
    bus.close(row.id)                        # 无论如何都执行
```

和 Java 的区别：

| | Java | Python |
|---|---|---|
| 受检异常 | 有，必须 `throws` 或 catch | **没有**，全是运行时异常 |
| 抛出 | `throw new X()` | `raise X()` |
| 保留原因 | `new X(cause)` | `raise X() from exc` |
| 类型 | `catch (IOException e)` | `except IOError as exc` |

### `raise ... from exc`

```python
raise HTTPException(status_code=409, detail=str(exc)) from exc
```

`from exc` 保留原始异常链，打印时会显示 "The above exception was the direct cause of..."。等于 Java 的 `initCause`。**不写 `from` 会丢掉根因**，ruff 会警告。

### 自定义异常

```python
class PathEscapeError(ValueError): ...       # 继承 ValueError
class InvalidTransition(ValueError):
    def __init__(self, current, target):
        self.current = current                # 异常也能带字段
        self.target = target
        allowed = ", ".join(sorted(...)) or "(终态)"
        super().__init__(f"不能从 {current} 变成 {target}；{current} 只允许 → {allowed}")
```

**这个异常值得学**：它不光说"不行"，还说"你现在在哪、能去哪"。错误信息里带上可行选项，排查时间少一半。

### 本项目的异常哲学

`ToolRegistry.call` 把所有异常都**转成返回值**：

```python
# src/repopilot/tools/base.py:89-98
except TimeoutError:
    result = ToolResult(tool=name, ok=False, error="tool timed out")
except PathEscapeError as exc:
    result = ToolResult(tool=name, ok=False, error=f"blocked by sandbox: {exc}")
...
except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the run
    result = ToolResult(tool=name, ok=False, error=f"{type(exc).__name__}: {exc}")
```

**理由**：一个工具炸了不能让整个 Agent 挂掉。异常在**边界处**被吸收成统一的 `ToolResult(ok=False)`，上层节点只需要看 `.ok`，永远不用写 try。

`# noqa: BLE001` 是告诉 ruff "我知道抓裸 Exception 通常不好，这里是故意的"。**注释里写了为什么**，这是好习惯。

---

## §14 `with`：自动关门

```python
with open(f) as fh:       # 出了块自动 close，哪怕中间抛异常
    ...
```

Java 对照：try-with-resources。

本项目的三个关键用法：

```python
async with self._semaphore:                       # 进来占一个名额，出去自动还
async with pool.acquire() as conn, conn.transaction():   # 借连接 + 开事务，出去自动归还+提交
with contextlib.suppress(TimeoutError):           # "超时就当没发生"，比 try/except/pass 干净
```

`contextlib.suppress(X)` 完全等价于：

```python
try:
    ...
except X:
    pass
```

但少三行、意图更明显。本项目用了三处：worker 的退避睡眠、reaper 循环、停机等待。

### 自己造上下文管理器

```python
# src/repopilot/db/pool.py:62
@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        yield conn                # ← yield 之前 = 进入时执行；之后 = 退出时执行
```

`yield` 那一行把连接"交出去"给 `with` 块用，块结束后代码从 `yield` 后面继续（这里没有后续代码，因为清理由内层 `async with` 负责）。

**Java 对照**：`@Transactional`，但边界是显式的、能一眼看到的。

---

## §15 装饰器

```python
@某个东西
def 函数():
    ...
```

等价于 `函数 = 某个东西(函数)`。**本质就是拿一个函数，返回一个（通常是包装过的）函数。**

比喻：给函数套一层壳。Java 对照：AOP 切面 / 注解 + 代理。区别是 Python 的装饰器是**显式的、看得见的普通函数**，没有反射魔法。

本项目用到的：

| 装饰器 | 作用 | 出处 |
|---|---|---|
| `@dataclass(slots=True)` | 自动生成构造器等 | `workspace/manager.py:24` |
| `@lru_cache` | 缓存返回值，本项目用来做单例 | `config.py:69` |
| `@asynccontextmanager` | 把 async 生成器变成 `async with` 能用的东西 | `db/pool.py:62` |
| `@property` / `@staticmethod` / `@classmethod` | 见 §11 | 多处 |
| `@router.get("/health")` | FastAPI 注册路由 | `api/routes.py:51` |
| `@tool(...)` | **本项目自己写的**，给函数贴上工具元信息 | `tools/base.py:105` |

### 读懂自己写的 `@tool`

```python
# src/repopilot/tools/base.py:105
def tool(name, description, risk, parameters=None):
    def decorator(fn):
        fn.__tool_spec__ = ToolSpec(name=name, description=description,
                                    risk=risk, fn=fn, parameters=parameters or {})
        return fn                 # ← 原样返回，没有包装
    return decorator
```

三层函数看着晕，拆开看：

1. `tool(name=..., ...)` 被调用，返回 `decorator` 这个函数。
2. Python 拿 `decorator` 去装饰被 `@` 修饰的函数：`list_files = decorator(list_files)`。
3. `decorator` 干的事只有一件：**给函数对象挂一个属性 `__tool_spec__`**，然后原样返回它。

所以 `@tool(...)` **不改变函数行为**，只是往上贴了张标签。标签在 `build_registry()` 时被读出来：

```python
# src/repopilot/tools/__init__.py:19
for fn in ALL_TOOLS:
    registry.register(fn.__tool_spec__)
```

**这就是「元数据和注册解耦」**：函数定义处声明自己是什么（名字、描述、风险等级、参数），注册在另一个地方统一做。Java 对照：`@Component` + 组件扫描，但这个只有 12 行代码，没有容器。

### `@lru_cache` 当单例用

```python
# src/repopilot/config.py:69
@lru_cache
def get_settings() -> Settings:
    ...
```

`lru_cache` 本意是缓存函数结果。这个函数**没有参数**，所以缓存永远命中同一条 —— 效果就是**整个进程只会构造一次 `Settings`**。

Java 对照：Spring 的单例 Bean，但这里只需要一行。

---

## §16 `Protocol`：不用继承的接口

```python
# src/repopilot/llm/base.py:15
class LLMClient(Protocol):
    name: str
    async def structured(self, *, system: str, user: str, schema: type[T]) -> T: ...
```

然后：

```python
class AnthropicLLM:        # ← 注意！没有写 (LLMClient)
    name = "anthropic"
    async def structured(self, *, system, user, schema): ...
```

`AnthropicLLM` **没有继承任何东西**，但它满足 `LLMClient`，因为它有同名同签名的方法。这叫**结构化类型 / 鸭子类型的静态版本**：「长得像鸭子就是鸭子，而且类型检查器会帮你验」。

Java 对照：Java 的 `interface` 是**名义类型** —— 必须写 `implements` 才算。Python 的 `Protocol` 不需要，**实现方甚至不需要知道协议的存在**。

**为什么本项目用它**：`ScriptedLLM`（测试替身）和 `AnthropicLLM`（真模型）互不知道对方，也都不 import `LLMClient`。图只依赖协议：

```python
def build_graph(llm: LLMClient, registry: ToolRegistry, workspace: Workspace):
```

换供应商 = 换一个满足协议的类，图一行不改。

面试口径：**"依赖倒置，但连接口都不用实现方去继承。"**

---

## §17 `TypedDict` / `Literal` / `Annotated` / `TypeVar`

### `TypedDict` —— 有类型的字典

```python
# src/repopilot/agent/state.py:17
class AgentState(TypedDict, total=False):
    run_id: str
    task: str
    analysis: Analysis | None
    ...
```

**它运行时就是一个普通的 `dict`**，没有任何校验、零开销。类型只存在于 IDE 和类型检查器眼里。

`total=False` = 所有键都是可选的（可以缺）。

Java 对照：`Map<String,Object>`，但编译器假装它有类型。

**为什么状态用 TypedDict 而不是 Pydantic 模型**：因为 LangGraph 内部就是把 state 当 dict 来合并的（节点返回半个 dict，框架 merge 进去）。用 Pydantic 每次合并都要重新构造对象 + 重新校验，白白付出成本，而这里的数据**不是来自外部**、不需要校验。

**一句话原则**：**边界上用 Pydantic 校验，内部用 TypedDict 保持轻快。**

### `Literal` —— 只能是这几个值之一

```python
Verdict = Literal["success", "retry", "failed"]
decision: Literal["approved", "rejected"]
risk: Risk = Literal["read", "write", "execute"]
```

Java 对照：小号的 enum。用在「就三个取值、不值得开个类」的场合。FastAPI 会把它变成 OpenAPI 的 enum，Swagger UI 上会显示成下拉框。

### `Annotated` —— 给类型贴附加信息

```python
# src/repopilot/agent/state.py:34
tool_calls: Annotated[list[ToolCallRecord], operator.add]
```

读作：「这是一个 `list[ToolCallRecord]`，另外**附带一条信息 `operator.add`**」。

类型本身还是列表，`operator.add` 是给 **LangGraph** 看的：合并 state 时这个键用加法（拼接）而不是覆盖。详见 [04-stack.md](04-stack.md) §4。

Java 对照：字段上的注解，`@Reducer(ADD) List<X> toolCalls`。

`operator.add` 是标准库里「加号的函数版」，`operator.add([1],[2]) == [1,2]`。

### `TypeVar` —— 泛型

```python
T = TypeVar("T", bound=BaseModel)

async def structured(self, *, system: str, user: str, schema: type[T]) -> T: ...
```

= Java 的 `<T extends BaseModel> T structured(..., Class<T> schema)`。

意思：**传进来什么模型类，就返回什么模型的实例**。所以调用处 IDE 知道返回的是 `Analysis` 而不是 `BaseModel`：

```python
analysis = await self.llm.structured(..., schema=Analysis)   # IDE 知道这是 Analysis
```

---

## §18 生成器：`yield`

普通函数 `return` 一次就结束；生成器函数 `yield` 一个值之后**暂停**，下次被要值时从暂停处继续。

比喻：`return` 是把整锅饭端出来；`yield` 是一勺一勺盛，你要一勺我盛一勺。

本项目最重要的用处是 **SSE 流式响应**：

```python
# src/repopilot/api/routes.py:234
async def event_stream() -> AsyncIterator[str]:
    try:
        while True:
            item = await queue.get()
            if not isinstance(item, RunEvent):
                yield "event: done\ndata: {}\n\n"
                return
            yield f"event: {item.type}\ndata: {item.model_dump_json()}\n\n"
    finally:
        bus.unsubscribe(run_id, queue)
```

`async def` + `yield` = **异步生成器**。FastAPI 拿到它以后，每 `yield` 一次就往 HTTP 连接里写一段，连接一直不关。

如果不用生成器，你就得先把所有事件收集完再一次性返回 —— 那就不叫流式了。

Java 对照：`Flux<String>` / `SseEmitter`。

---

## §19 五个必须知道的坑

### 坑 1：可变默认参数

```python
def f(items=[]):        # ❌❌❌ 千万别
    items.append(1)
    return items

f()   # [1]
f()   # [1, 1]   ← 同一个列表！默认值只在函数定义时创建一次
```

正解，也是本项目的写法：

```python
meta: dict[str, Any] = Field(default_factory=dict)      # Pydantic
parameters: dict[str, Any] = field(default_factory=dict) # dataclass
def f(items=None):
    items = items if items is not None else []
```

`default_factory` = 「每次要用的时候现造一个」。

### 坑 2：`if x:` 不等于 `if x is not None:`

见 §4。`0`、`""`、`[]` 都是假值。

### 坑 3：改列表的同时遍历它

会漏元素。要改就先复制：`for x in list(lst):`

### 坑 4：`is` vs `==`

`is` 比较身份，`==` 比较值。**只对 `None`、`True`、`False` 用 `is`**，别的一律 `==`。

### 坑 5：闭包捕获的是变量本身，不是值

```python
fns = [lambda: i for i in range(3)]
[f() for f in fns]     # [2, 2, 2] 而不是 [0, 1, 2]
```

Java 强制 lambda 捕获的变量必须 effectively final，正是为了堵这个。Python 不管，得自己小心（`lambda i=i: i` 可以绕）。本项目没踩这个坑，但循环里造闭包时要警惕。

---

## §20 `if __name__ == "__main__"`

```python
if __name__ == "__main__":
    asyncio.run(main())
```

意思：「**只有当这个文件是被直接运行的时候**才执行下面这段，被 import 的时候不执行」。

`__name__` 是每个模块自带的变量：直接运行时是 `"__main__"`，被 import 时是模块名。

Java 对照：`public static void main`，但 Java 是靠 JVM 挑一个类的 main 方法；Python 是每个文件都可以有 main 块。

`scripts/demo.py` 用的就是这个。

---

## §21 一张速查表

| 你想干的事 | Python | Java |
|---|---|---|
| 判空 | `if not lst:` | `lst.isEmpty()` |
| 判 null | `if x is None:` | `x == null` |
| 拼字符串 | `"\n".join(lst)` | `String.join("\n", lst)` |
| 格式化 | `f"{a} {b}"` | `String.format` |
| map | `[f(x) for x in lst]` | `stream().map().toList()` |
| filter | `[x for x in lst if p(x)]` | `stream().filter().toList()` |
| anyMatch | `any(p(x) for x in lst)` | `stream().anyMatch()` |
| 取字典默认值 | `d.get(k, default)` | `map.getOrDefault(k, d)` |
| 静态工厂 | `@classmethod def of(cls)` | `static X of()` |
| 接口 | `Protocol` | `interface` |
| 枚举 | `StrEnum` | `enum` |
| 值对象 | `@dataclass` / `BaseModel` | record / Lombok |
| try-with-resources | `with` | `try (...)` |
| 单例 | `@lru_cache` 的无参函数 | `@Bean` / 饿汉式 |
| 泛型方法 | `TypeVar` | `<T>` |
| AOP | 装饰器 | 注解 + 代理 |
| ThreadLocal | `ContextVar` | `ThreadLocal` / MDC |
| Semaphore | `asyncio.Semaphore` | `java.util.concurrent.Semaphore` |
| CompletableFuture.allOf | `asyncio.gather` | 同 |
| Future.get(timeout) | `asyncio.wait_for` | 但 wait_for 会真的取消 |

---

下一章：[03-asyncio.md](03-asyncio.md) —— 异步是这个项目的骨架，值得单开一章。
