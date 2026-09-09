.PHONY: sync db-up db-down db-reset psql test test-fast test-nodb lint fmt run demo clean

# uv 在这台机器上写出来的 .pth 带 macOS UF_HIDDEN 标志，而 CPython 的 site.py
# 会静默跳过隐藏的 .pth -> import repopilot 失败。详见 docs/failures.md。
# 永远用这个目标同步依赖，不要直接 uv sync。
sync:
	uv sync
	@chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true

# ------------------------------------------------------------------ 数据库
db-up:
	docker compose up -d
	@until docker exec repopilot-pg pg_isready -U repopilot -d repopilot >/dev/null 2>&1; \
	  do echo "等 postgres 起来…"; sleep 1; done
	@echo "postgres 就绪: localhost:5433"

db-down:
	docker compose down

# 删库重建。schema.sql 改了之后用这个。
db-reset:
	docker compose down -v
	$(MAKE) db-up

psql:
	docker exec -it repopilot-pg psql -U repopilot -d repopilot

# ------------------------------------------------------------------ 测试
test: sync db-up
	uv run pytest

test-fast: sync db-up
	uv run pytest -m "not slow"

# 不碰数据库的那部分，最快的反馈循环
test-nodb: sync
	uv run pytest tests/test_status.py tests/test_tools.py tests/test_workspace.py \
	              tests/test_search_code.py tests/test_graph.py

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

# ------------------------------------------------------------------ 运行
run: sync db-up
	uv run uvicorn repopilot.api.app:app --reload --port 8000

demo: sync
	uv run python scripts/demo.py

clean:
	rm -rf .workspaces .pytest_cache .ruff_cache
