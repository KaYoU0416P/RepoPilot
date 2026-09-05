"""Isolated per-run copy of the target repository.

Security core of the MVP: every path an LLM produces goes through Workspace.resolve(),
which refuses to leave the workspace root. The agent never touches the host repo.
"""

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from repopilot.observability import get_logger

log = get_logger(__name__)

_IGNORED = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules", ".ruff_cache"}


class PathEscapeError(ValueError):
    """Raised when a tool argument tries to reach outside the workspace."""


@dataclass(slots=True)
class Workspace:
    run_id: str
    root: Path

    def resolve(self, relative_path: str) -> Path:
        """Map an agent-supplied path to a real path, or refuse.

        Blocks absolute paths, `..` traversal and symlinks that point outside root.
        """
        candidate = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise PathEscapeError(f"path escapes workspace: {relative_path!r}")
        return candidate

    def relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root.resolve()))

    def iter_files(self, pattern: str = "**/*") -> list[Path]:
        out: list[Path] = []
        for p in sorted(self.root.glob(pattern)):
            if not p.is_file():
                continue
            if any(part in _IGNORED for part in p.parts):
                continue
            out.append(p)
        return out

    def tree(self, limit: int = 200) -> str:
        return "\n".join(self.relative(p) for p in self.iter_files()[:limit])


class WorkspaceManager:
    """Creates and destroys per-run workspaces."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, source_repo: Path, run_id: str | None = None) -> Workspace:
        run_id = run_id or uuid.uuid4().hex[:12]
        target = self.root / run_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            source_repo,
            target,
            ignore=shutil.ignore_patterns(*_IGNORED),
        )
        ws = Workspace(run_id=run_id, root=target)
        self._git_init(ws)
        log.info("workspace created at %s from %s", target, source_repo)
        return ws

    def cleanup(self, ws: Workspace) -> None:
        shutil.rmtree(ws.root, ignore_errors=True)
        log.info("workspace cleaned up: %s", ws.root)

    @staticmethod
    def _git_init(ws: Workspace) -> None:
        """Baseline commit so `git diff` later shows exactly what the agent changed."""
        env = {
            "GIT_AUTHOR_NAME": "repopilot",
            "GIT_AUTHOR_EMAIL": "agent@repopilot.local",
            "GIT_COMMITTER_NAME": "repopilot",
            "GIT_COMMITTER_EMAIL": "agent@repopilot.local",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        }
        for cmd in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "baseline", "--no-gpg-sign"],
        ):
            subprocess.run(cmd, cwd=ws.root, env=env, check=True, capture_output=True)
