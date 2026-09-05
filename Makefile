.PHONY: sync test lint fmt run demo clean

# `uv sync` on this machine writes .pth files with the macOS UF_HIDDEN flag, and
# CPython's site.py silently skips hidden .pth files -> `import repopilot` fails.
# See docs/failures.md. Always sync through this target.
sync:
	uv sync
	@chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true

test: sync
	uv run pytest

test-fast: sync
	uv run pytest -m "not slow"

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

run: sync
	uv run uvicorn repopilot.api.app:app --reload --port 8000

demo: sync
	uv run python scripts/demo.py

clean:
	rm -rf .workspaces .pytest_cache .ruff_cache
