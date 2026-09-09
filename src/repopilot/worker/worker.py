"""后台 worker：不停地从队列领任务、执行、回写。

限流有两层，别混淆：
  1. **进程内并发上限**（这里的 Semaphore）—— 同时最多跑几个 Agent。
     防的是本机 CPU / 内存 / 子进程被打爆。
  2. **工具级并发上限**（ToolRegistry 里的 Semaphore）—— 单个 Agent 内同时几个工具。
     防的是一个 Agent 自己把文件句柄用光。

两层是乘的关系：最坏情况 = max_concurrent_runs × max_concurrent_tools。
"""

import asyncio
import contextlib
import os
import socket

from repopilot.config import Settings
from repopilot.db import runs as runs_repo
from repopilot.observability import get_logger
from repopilot.worker.bus import EventBus
from repopilot.worker.runner import Runner

log = get_logger(__name__)


def default_worker_id() -> str:
    """机器名 + 进程号。拆多进程/多机部署时用来区分是谁持有租约。"""
    return f"{socket.gethostname()}-{os.getpid()}"


class Worker:
    def __init__(self, settings: Settings, bus: EventBus, worker_id: str | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.worker_id = worker_id or default_worker_id()
        self.runner = Runner(settings, bus)
        self._slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self._inflight: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        log.info(
            "worker %s 启动 (并发上限=%s, 租约=%ss)",
            self.worker_id,
            self.settings.max_concurrent_runs,
            self.settings.lease_seconds,
        )
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
                    # 空转就退避。生产环境可换成 PG 的 LISTEN/NOTIFY，做到零延迟。
                    await self._sleep_or_stop(self.settings.poll_interval_seconds)
                    continue

                task = asyncio.create_task(self._guarded_execute(row), name=f"run-{row.id}")
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        finally:
            reaper.cancel()
            await self._drain()

    async def stop(self) -> None:
        """优雅停机：不再领新任务，等在跑的跑完。"""
        self._stopping.set()

    async def _guarded_execute(self, row) -> None:
        try:
            await self.runner.execute(row, self.worker_id)
        finally:
            self._slots.release()

    async def _reaper_loop(self) -> None:
        """定期清理重试用尽的僵尸任务。"""
        while True:
            await asyncio.sleep(self.settings.lease_seconds)
            with contextlib.suppress(Exception):
                await runs_repo.reap_exhausted()

    async def _sleep_or_stop(self, seconds: float) -> None:
        """睡一会儿，但收到停机信号立刻醒 —— 别让停机等满一个轮询周期。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _drain(self) -> None:
        if not self._inflight:
            return
        log.info("等待 %s 个在跑的任务收尾…", len(self._inflight))
        done, pending = await asyncio.wait(
            self._inflight, timeout=self.settings.shutdown_grace_seconds
        )
        for task in pending:
            task.cancel()  # 超时还没完的，取消掉；租约会让任务回到队列
        log.info("停机完成 (完成=%s, 取消=%s)", len(done), len(pending))
