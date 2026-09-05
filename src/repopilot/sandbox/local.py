"""Local subprocess sandbox: no shell, fixed cwd, hard timeout, process-group kill.

Stage 1 of the sandbox story. Stage 2 (Docker) swaps only this module's
implementation, because everything above depends on `run_command`'s signature.
"""

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from repopilot.observability import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


async def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 64_000,
) -> CommandResult:
    """Run a command with no shell interpretation and a wall-clock timeout.

    `start_new_session=True` puts the child in its own process group so that on
    timeout we can kill the whole tree, not just the direct child (pytest spawns
    subprocesses; killing only the parent leaves orphans holding the workspace).
    """
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
        start_new_session=True,
    )

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        _kill_process_group(proc.pid)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            stdout, stderr = b"", b""

    duration_ms = int((time.perf_counter() - started) * 1000)
    result = CommandResult(
        command=command,
        exit_code=-1 if timed_out else (proc.returncode or 0),
        stdout=_truncate(stdout, max_output_bytes),
        stderr=_truncate(stderr, max_output_bytes),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
    log.info(
        "cmd %s exit=%s timed_out=%s %dms",
        " ".join(command[:3]),
        result.exit_code,
        timed_out,
        duration_ms,
    )
    return result


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _truncate(raw: bytes, limit: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - limit} chars truncated]...\n{text[-half:]}"
