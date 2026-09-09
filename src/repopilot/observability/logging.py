"""Structured-ish logging with a run_id carried in a ContextVar.

ContextVar is the asyncio equivalent of Java's ThreadLocal: each Task inherits a
copy of the context, so concurrent runs do not leak run_ids into each other.
"""

import logging
import sys
from contextvars import ContextVar

run_id_var: ContextVar[str] = ContextVar("run_id", default="-")


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        return True


def setup_logging(level: int = logging.INFO, stream=None) -> None:
    """装配根 logger。

    `stream` 默认 stdout，但 **MCP server 必须传 sys.stderr**：
    stdio 传输下 stdout 就是 JSON-RPC 的协议通道，往里写一行日志
    就等于给对端发了一条畸形消息，连接直接废掉。
    这是 MCP stdio server 最经典的坑，见 `mcp/server.py`。
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [run=%(run_id)s] %(name)s | %(message)s")
    )
    handler.addFilter(_RunIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
