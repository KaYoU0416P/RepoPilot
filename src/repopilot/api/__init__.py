"""HTTP 层。

**这里刻意不 re-export `app` / `create_app`。**

原本是 `from repopilot.api.app import app, create_app`，结果是一个循环：

    worker/__init__ → worker.bus → api.schemas → api/__init__（← 就是这一行）
                    → api.app → api.routes → worker（还没初始化完）→ ImportError

`worker.bus` 只想要 `RunEvent` 这一个 DTO，却因为包的 `__init__` 把整个
FastAPI 应用连带路由一起拽了进来。之前没炸，纯粹是因为所有入口都恰好先
import 了 `repopilot.api`；换个 import 顺序就崩 —— 这种「靠运气不崩」的状态
迟早要出事，加个测试文件就能踩到（就是这么发现的）。

规矩：**包的 `__init__` 不做有副作用的导入。** 要 app 就写
`from repopilot.api.app import create_app`，路径长一点，换掉一个隐形地雷。
uvicorn 用的 `repopilot.api.app:app` 本来就是子模块路径，不受影响。
"""
