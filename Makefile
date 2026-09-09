.PHONY: sync db-up db-down db-reset psql test test-fast test-nodb bench bench-check mcp mcp-smoke lint fmt run demo clean

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

# ------------------------------------------------------------------ 评测
# 跑 15 个 seeded bug，出成功率 / 重试 / 工具分布 / 失败原因报表。
# 没有 ANTHROPIC_API_KEY 时会降级到 ScriptedLLM，那时分数没有意义，
# 只能验证 harness 通不通 —— 脚本自己会警告。
bench: sync
	uv run python scripts/bench.py --json bench-report.json

# 只体检评测集本身：每个 case 的 bug 是不是真的种进去了、
# 参考答案能不能过隐藏测试。不调 LLM，不花钱。
bench-check: sync
	uv run pytest tests/test_bench.py -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

# ------------------------------------------------------------------ 运行
run: sync db-up
	uv run uvicorn repopilot.api.app:app --reload --port 8000

demo: sync
	uv run python scripts/demo.py

# MCP server（stdio）。手动起一般只是为了看它没崩 —— 正常用法是让
# Claude Desktop / Claude Code 去 spawn 它，配置见 scripts/mcp_server.py 头注释。
# 依赖 sync：新建源码文件会让 uv 重装 editable 包，.pth 的 UF_HIDDEN 会复发。
mcp: sync
	uv run --no-sync python scripts/mcp_server.py --repo $(or $(REPO),fixtures/sample_repo)

# 不起客户端，直接打一轮 stdio 握手，确认协议没坏。
mcp-smoke: sync
	@printf '%s\n%s\n%s\n' \
	  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
	  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
	  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
	| uv run --no-sync python scripts/mcp_server.py --repo fixtures/sample_repo 2>/dev/null \
	| python3 -c 'import sys,json; ls=[l for l in sys.stdin.read().strip().split("\n") if l]; \
	  assert len(ls)==2, "通知不该回包"; [json.loads(l) for l in ls]; \
	  print("MCP stdio 握手正常：%d 行响应，stdout 未被日志污染" % len(ls))'

clean:
	rm -rf .workspaces .pytest_cache .ruff_cache
