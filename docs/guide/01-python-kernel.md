# 01 · Python 到底是个什么东西（内核篇 + 目录结构）

这一章回答四个问题：
1. 我敲 `python xxx.py`，机器里发生了什么？
2. `import repopilot` 是怎么找到文件的？
3. `.venv`、`uv`、`pyproject.toml` 各是什么？
4. 本项目每个文件夹为什么长这样？

---

## §1 从源码到运行：Python 和 Java 走的是同一条路

你写 Java 的时候是这样的：

```
Hello.java  --javac-->  Hello.class（字节码）  --JVM-->  跑起来
             你手动敲                            java 命令
```

Python 一模一样，只是**两步合成了一步，你看不见中间产物**：

```
hello.py  --(自动)-->  字节码  --CPython 虚拟机-->  跑起来
                        存在 __pycache__/hello.cpython-312.pyc
```

所以：

- **Python 不是「解释执行源码」**。它也编译，编译成字节码，只是编译这一步是运行时自动做的、快到你察觉不到。
- 你看到的 `__pycache__/` 文件夹里那些 `.pyc`，就是 Python 版的 `.class`。删掉没事，下次自动再生成。存在的意义只有一个：第二次 `import` 同一个模块时不用重新编译，启动快一点。
- **CPython** = 官方那个用 C 写的 Python 实现，就是你电脑上 `python` 这个命令。相当于 Java 世界的 HotSpot JVM。还有 PyPy、Jython 等别的实现，跟你没关系。

**一个关键差别**：Java 的 `.class` 是跨版本相对稳定的，Python 的 `.pyc` 是**绑死小版本**的 —— 文件名里那个 `cpython-312` 就是 Python 3.12 的意思。3.13 的 Python 读不了 3.12 的 `.pyc`，会重新编译。这就是为什么换 Python 版本必须重建虚拟环境。

**验证一下**（可以真的敲）：

```bash
uv run python -c "import dis; dis.dis(lambda a, b: a + b)"
```

会打出 `LOAD_FAST a / LOAD_FAST b / BINARY_OP + / RETURN_VALUE` 这样的东西 —— 这就是字节码，和你用 `javap -c` 看 Java 字节码是一件事。

---

## §2 「一切皆对象」和「变量是标签，不是盒子」

这是 Java 转 Python 最容易翻车的一个认知点，讲透它能省你后面十个疑问。

### 2.1 变量不是盒子

Java 里你会想：`int x = 5` 是「开一个叫 x 的盒子，把 5 放进去」。

Python 里**反过来**：`x = 5` 是「内存里有一个 5 这个对象，给它贴一张写着 x 的**便利贴**」。

```python
a = [1, 2, 3]
b = a          # 不是复制！是给同一个列表又贴了一张叫 b 的便利贴
b.append(4)
print(a)       # [1, 2, 3, 4]  ← a 也变了，因为 a 和 b 贴的是同一个东西
```

这跟 Java 的**引用类型**行为完全一样（`List<Integer> b = a;` 也是这个结果）。区别是 Java 有 `int` / `double` 这种基本类型作为例外，**Python 一个例外都没有**：数字、字符串、函数、类、模块，全是堆上的对象，变量全是引用。

所以下面这句在 Python 里是完全合法的，而且本项目真的这么用：

```python
# src/repopilot/tools/__init__.py:9
ALL_TOOLS = [list_files, read_file, search_code, write_file, git_diff, run_tests]
```

`list_files` 是一个**函数**，这里把六个函数塞进一个列表。函数是对象，能进列表、能当参数传、能当返回值。Java 要用 `Function` / 方法引用 / 匿名类才能做到，Python 里天生如此。

### 2.2 可变 vs 不可变

| 类型 | 可变吗 | 说明 |
|---|---|---|
| `int` `float` `str` `bool` `tuple` `frozenset` | **不可变** | 改不了，只能造个新的 |
| `list` `dict` `set` 和你自己定义的类 | 可变 | 原地能改 |

`x = 5; x = 6` 不是「把盒子里的 5 改成 6」，是「把 x 这张便利贴从 5 撕下来贴到 6 上」。

这条规则直接解释了一个大坑（见 [02-syntax.md](02-syntax.md) §19「可变默认参数」），也解释了本项目为什么在状态机里用 `frozenset`：

```python
# src/repopilot/domain/status.py:33
TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {...}
```

`frozenset` = 不可变的集合。**这张表是全项目状态流转的唯一真相来源，任何人不小心 `.add()` 一下都是灾难**，所以用不可变类型从根上堵死。Java 对照：`Collections.unmodifiableSet()` / `Set.of()`。

### 2.3 内存怎么回收

Python 主要靠**引用计数**：每个对象记着「有几张便利贴贴在我身上」，归零立刻销毁。

- 优点：确定性强，对象一没人用**马上**就释放，不用等 GC 心情好。
- 缺点：循环引用（A 指向 B，B 指向 A，但外面没人要它们）计数永远不为 0。所以 Python 另外还有一个**分代 GC** 专门扫循环引用，跟 Java 的分代 GC 是同一个思路。

**为什么你要知道这个**：因为它解释了 `with` 语句为什么在 Python 里不像 Java 那么"必须"——但**你仍然必须用**。文件对象在没人引用时会自动关，可你无法保证时机（尤其是异常路径），所以本项目所有资源都走 `with` / `async with`。

---

## §3 GIL：为什么这个项目用 asyncio 而不是线程池

**GIL（全局解释器锁）** 一句话：CPython 里有一把全局大锁，**任何时刻只有一个线程能在执行 Python 字节码**。

比喻：一个厨房，十个厨师，但**只有一把菜刀**。谁拿到刀谁切菜，其他人干等。

后果：

| 任务类型 | Java 多线程 | Python 多线程 |
|---|---|---|
| CPU 密集（算数、压缩） | 真并行，8 核跑满 | **没用**，还是一个核，还多了切换开销 |
| I/O 密集（读文件、查库、发 HTTP） | 有效 | 有效（等 I/O 时会**放开**那把刀） |

关键点：线程在**等 I/O 的时候会释放 GIL**。所以 Python 多线程对 I/O 有用，对 CPU 没用。

那既然 I/O 有用，为什么本项目不用线程池？因为：

1. **线程贵**。一个 OS 线程默认 8MB 栈，创建/切换要进内核。一个协程（coroutine）只是堆上一个小对象，几 KB，切换纯用户态。
2. **线程要加锁**。多线程共享 state 就得考虑竞态。协程在两个 `await` 之间是**绝对不会被打断**的，所以像 `seq += 1` 这种操作天然安全。
3. 我们的活**全是 I/O**：读文件、跑 pytest 子进程、查 Postgres、调 LLM 的 HTTP 接口。CPU 几乎不干活。

**面试口径**：
> "Agent 的工作是 I/O 密集的 —— 文件读写、子进程、数据库、LLM HTTP 调用。这种场景要的是**并发**不是**并行**，一个事件循环就能把吞吐打满，还省掉线程池的内存和加锁成本。而且 CPython 有 GIL，多线程在 CPU 密集场景本来也没有加速。"

（补充一句给自己听：Python 3.13 开始有实验性的「无 GIL」构建，3.14 转正为官方支持的可选构建。但本项目用的是 3.12，标准带 GIL 的版本。面试被问到可以提一嘴，显得跟得上。）

---

## §4 `import` 到底怎么找文件（这是本项目踩过大坑的地方）

### 4.1 模块和包

- **模块（module）** = 一个 `.py` 文件。`repopilot/config.py` → 模块 `repopilot.config`。
- **包（package）** = 一个装着 `.py` 的文件夹。`repopilot/db/` → 包 `repopilot.db`。
- **`__init__.py`** = 包的「门面」。文件夹里有这个文件，Python 就把它当包；这个文件里写的东西，就是 `import repopilot.db` 时会执行的内容。

Java 对照：包 ≈ package，`__init__.py` ≈ 没有直接对应物，最接近的是「一个 package-info.java + 一堆 re-export」。

看本项目的例子：

```python
# src/repopilot/tools/__init__.py:5
from repopilot.tools.fs_tools import list_files, read_file, search_code, write_file
```

这叫 **re-export（转出口）**。作用是让外面可以写 `from repopilot.tools import read_file`，而不用关心它其实住在 `fs_tools.py` 里。好处是**内部文件怎么拆分是自由的，对外的名字稳定**。Java 里你做不到这件事（package 结构就是对外契约）。

### 4.2 `sys.path`：Python 的 classpath

`import repopilot` 时，Python 依次去 `sys.path` 这个列表里的每个目录找。它 ≈ Java 的 `CLASSPATH`。

`sys.path` 从哪来：
1. 当前脚本所在目录（或 `python -c` 时的当前工作目录）
2. `PYTHONPATH` 环境变量
3. **虚拟环境的 `site-packages` 目录**（第三方包住在这）
4. `site-packages` 里所有 `.pth` 文件里写的额外路径 ← **重点，下面 §6 会炸**

自己看一眼：

```bash
uv run python -c "import sys; print('\n'.join(sys.path))"
```

### 4.3 绝对导入 vs 相对导入

本项目**统一用绝对导入**：

```python
from repopilot.db import runs as runs_repo    # ✅ 绝对，从包根开始写全
from ..db import runs                          # ❌ 相对，本项目不用
```

理由：绝对导入在任何地方复制粘贴都不会错，相对导入换个位置就崩。团队项目里绝对导入是主流。

`as runs_repo` 是**起别名**，因为 `runs` 这个名字太容易和局部变量撞车。Java 对照：没有，Java 只能靠全限定名。

### 4.4 循环导入（要知道，因为你迟早会撞）

A 导入 B，B 又导入 A → `ImportError: cannot import name ... (most likely due to a circular import)`。

Java 允许类互相引用（编译期解析），Python 不行，因为 `import` 是**运行时真的去执行那个文件**。执行到一半又回来找自己，就拿到半成品。

解法：把公共的东西抽到第三个模块。本项目的 `domain/status.py` 就是这么设计的 —— `db/`、`api/`、`worker/` 都依赖它，它**谁都不依赖**（只 import 标准库）。这不是巧合，是分层。

---

## §5 虚拟环境、uv、pyproject.toml

### 5.1 `.venv/` 是什么

**问题**：项目 A 要 `fastapi 0.115`，项目 B 要 `fastapi 0.90`。装哪个？

**Java 的答案**：不存在这个问题，Maven 把 jar 装在 `~/.m2`，每个项目的 `pom.xml` 各写各的版本，编译时挑。

**Python 的答案**：给每个项目开一个独立的「小 Python 安装」，叫**虚拟环境**，就是那个 `.venv/` 文件夹。

```
.venv/
  bin/python          ← 这个项目专用的 python（其实是个软链接）
  bin/pytest          ← 装进来的命令行工具
  lib/python3.12/site-packages/    ← 第三方包全在这，fastapi、pydantic、asyncpg…
```

**关键认知**：`.venv` 不是配置，是**一堆真实的文件**，能删能重建，不该进 git（`.gitignore` 里已经忽略了）。删了跑 `make sync` 就回来。

「激活虚拟环境」`source .venv/bin/activate` 干的事只有一件：把 `.venv/bin` 塞到 `PATH` 最前面，让你敲 `python` 时命中的是这个。**本项目不需要激活**，因为我们统一用 `uv run xxx`，它自动用 `.venv`。

### 5.2 `uv` 是什么

`uv` = 用 Rust 写的 Python 包管理器，比老的 `pip` 快一个数量级。

| uv 命令 | Maven 对照 |
|---|---|
| `uv add fastapi` | 往 pom.xml 加 dependency 并下载 |
| `uv sync` | `mvn dependency:resolve`，按锁文件把环境弄成该有的样子 |
| `uv run pytest` | `mvn test`，在项目环境里跑命令 |
| `uv.lock` | 锁定版本的文件 ≈ 精确到传递依赖的 BOM |

### 5.3 `pyproject.toml` = pom.xml

本项目的关键几段：

```toml
requires-python = ">=3.12"          # 相当于 maven.compiler.source
dependencies = [                     # 运行时依赖
    "fastapi>=0.115",
    "asyncpg>=0.31.0",
    ...
]

[dependency-groups]
dev = ["pytest", "ruff", ...]        # 相当于 <scope>test</scope>

[tool.pytest.ini_options]
asyncio_mode = "auto"                # 让 async def 的测试函数不用加装饰器就能跑
testpaths = ["tests"]
pythonpath = ["src"]                 # ← 见下面 §6，这是兜底补丁

[tool.ruff]
line-length = 100                    # ruff = linter + formatter，相当于 CheckStyle + Spotless
```

`[tool.xxx]` 段是各个工具自己的配置区，写在同一个文件里而不是散落成 `.pytest.ini`、`.ruff.toml`。

---

## §6 ⚠️ 本机专属大坑：macOS 隐藏标志 + `.pth` 文件

这是本项目**踩过、并且每次 `uv add` 之后都会复发**的坑。必须理解，否则你会突然遇到「明明装了却 import 不到」。

### 6.1 「可编辑安装」是什么

我们的代码在 `src/repopilot/`，但 `src/` 不在 `sys.path` 里。凭什么能 `import repopilot`？

答案：项目自己也被「安装」进了虚拟环境，而且是**可编辑安装（editable install）** —— 不是把代码复制进 `site-packages`，而是在 `site-packages` 里放一个小文件 `_repopilot.pth`，内容就是一行路径：

```
/Users/kayou/Documents/py_agent/src
```

Python 启动时会执行 `site` 模块，它扫描 `site-packages` 下所有 `.pth` 文件，把里面的路径**追加进 `sys.path`**。于是 `src/` 进了搜索路径，`import repopilot` 就通了。

好处是改代码立刻生效，不用重新安装。Java 对照：≈ IDE 把 `target/classes` 直接挂上 classpath，而不是每次打 jar。

### 6.2 坑在哪

macOS 的文件有一个叫 `UF_HIDDEN` 的标志位（和 Unix 的「点开头文件」是两回事）。**这台机器上 `uv` 写出来的文件都带这个标志**。

而 CPython 的 `site.addpackage()` 在读 `.pth` 时会**静默跳过隐藏文件** —— 不报错、不警告，就当没看见。

结果：

```
uv pip list          →  repopilot 0.1.0 (editable)   看起来一切正常
python -c "import repopilot"  →  ModuleNotFoundError  ← ？？？
```

**排查它的关键线索**（当时就是靠这个定位的）：

```bash
uv run python -c "import sys; print('_virtualenv' in sys.modules)"
```

正常虚拟环境里这个应该是 `True`（因为 `_virtualenv.pth` 被执行了）。如果是 `False`，说明**所有 `.pth` 都被跳过了**，问题不在你的包，在整个 `.pth` 机制。

### 6.3 解法

`Makefile` 里的 `sync` 目标每次都把标志清掉：

```makefile
sync:
	uv sync
	@chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true
```

**所以：永远用 `make sync`，不要直接敲 `uv sync`。**

另外 `pyproject.toml` 里的 `pythonpath = ["src"]` 是给 pytest 的**第二道保险** —— 就算 `.pth` 失效，pytest 也能跑。但 **uvicorn 不吃这个配置，`make run` 照样会炸**。

完整复盘见 [../failures.md](../failures.md)。

**这件事本身就是个面试素材**：「一个静默失败的第三方行为，靠对比标准库应有的副作用来定位」，比背八股有意思。

---

## §7 本项目目录结构：每个文件夹在干什么

```
py_agent/
├── pyproject.toml          项目定义 + 依赖 + 工具配置（= pom.xml）
├── uv.lock                 锁定的精确版本（别手改）
├── Makefile                所有常用命令的入口，你只需要记 make xxx
├── docker-compose.yml      起 PostgreSQL 的编排文件
├── .env.example            环境变量样例（真正的 .env 不进 git）
├── .venv/                  虚拟环境，不进 git，可删可重建
│
├── db/
│   └── schema.sql          建表 DDL。容器首次启动时自动执行
│
├── fixtures/
│   └── sample_repo/        Agent 的练习靶子：一个故意写错的小仓库
│       ├── calculator.py       有 bug 的实现
│       └── test_calculator.py  能暴露 bug 的测试
│
├── scripts/
│   └── demo.py             不起服务、不连数据库，直接跑一次 Agent
│
├── src/repopilot/          ← 真正的代码。src 布局，见下面 §7.1
│   ├── config.py           全局配置（读环境变量）
│   ├── domain/             业务规则核心：状态机。不依赖任何其他内部模块
│   ├── db/                 数据访问层（连接池、SQL、行映射）
│   ├── api/                HTTP 层（路由、DTO、SSE）
│   ├── worker/             后台执行层（领任务、跑、事件总线）
│   ├── agent/              Agent 本体（LangGraph 图、节点、提示词、结构化输出模型）
│   ├── tools/              Agent 能用的六个工具 + 统一的调用护栏
│   ├── workspace/          仓库副本 + 路径收敛（安全核心）
│   ├── sandbox/            带硬超时的子进程执行
│   ├── llm/                大模型适配层（真模型 / 测试替身）
│   ├── github/             GitHub webhook 验签与解析 + REST 客户端
│   ├── publishing/         开 PR / 回写 Issue 评论（真发 / 无 token 时空转）
│   ├── evaluation/         轨迹指标（成功率、工具分布、重试次数）
│   └── observability/      日志 + run_id 上下文
│
├── tests/                  pytest 测试
│   └── conftest.py         测试的公共装置（fixture）
│
└── docs/                   架构、进度、面试笔记、故障复盘、本教程
```

### 7.1 为什么是 `src/` 布局

代码放在 `src/repopilot/` 而不是根目录的 `repopilot/`，这叫 **src layout**，是现在 Python 社区的推荐做法。

理由：如果代码在根目录，你在项目根目录下敲 `python` 时，当前目录会自动进 `sys.path`，于是 `import repopilot` 命中的是**源码目录**而不是**安装好的包**。这会掩盖「我忘了把某个文件加进打包配置」这类错误 —— 本地一切正常，用户装上就缺文件。

src 布局强制你**只能通过安装来 import**，本地跑的就是用户装的。

Java 对照：这就是 Maven 的 `src/main/java`。同一个道理，Python 只是晚了十几年才想通。

### 7.2 分层依赖方向（重要，面试会问「你怎么分层的」）

```
api/  ─┐
       ├─▶ db/ ─▶ domain/          domain 谁都不依赖，是最内核
worker/┘    │
  │         └─▶ pool（asyncpg）
  └─▶ agent/ ─▶ tools/ ─▶ workspace/
                  │          └─▶ sandbox/
                  └─▶ llm/
```

一条铁律：**箭头永远朝内，`domain/` 不能 import 任何内部模块**。所以状态机可以被单独测试，不用起数据库、不用起服务。`tests/test_status.py` 就是纯内存跑的。

Java 对照：这就是六边形架构 / 洋葱架构里「领域层不依赖基础设施」那一条。

---

## §8 这一章的面试口径

**Q: Python 是解释型语言吗？**
> 严格说是「编译成字节码后由虚拟机执行」，和 Java 是同一个模型，区别在于编译这一步是运行时隐式做的，产物缓存在 `__pycache__`。真正的差别是 Python 的类型检查在运行时，Java 在编译期。

**Q: GIL 是什么？对你的项目有影响吗？**
> GIL 保证同一时刻只有一个线程执行字节码，所以多线程对 CPU 密集没加速。但我这个项目全是 I/O —— 文件、子进程、数据库、LLM HTTP，所以我用 asyncio 单线程事件循环：并发够用，还省掉线程栈内存和锁。真需要压 CPU 的话会用多进程或者把活丢给子进程，我的 sandbox 本来就是子进程。

**Q: 为什么代码放 src 目录？**
> 防止「本地能 import 但装出来缺文件」这类错误。src 布局强制走安装路径，等价于 Maven 的 src/main/java。

**Q: 讲一个你排查过的诡异问题。**
> （讲 §6 那个 `.pth` + macOS 隐藏标志的故事。重点讲**怎么定位的**：`uv pip list` 显示正常但 import 失败，说明包管理器视角和解释器视角不一致；于是去查解释器加载路径的机制，发现 `.pth` 应该在启动时被 `site` 模块执行，用「虚拟环境自带的 `_virtualenv.pth` 有没有生效」当探针，一测发现它也没生效，问题就从「我的包坏了」变成「整个 `.pth` 机制被跳过了」，再去读 CPython `site.py` 源码就找到了那个静默跳过隐藏文件的分支。）

---

下一章：[02-syntax.md](02-syntax.md) —— 高频语法大全。
