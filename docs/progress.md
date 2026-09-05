# Progress

## DONE

- Project skeleton: uv + pyproject, src layout, ruff, pytest (asyncio_mode=auto).
- `workspace/`: per-run `copytree` + git baseline commit, `resolve()` path confinement.
- `sandbox/local.py`: no-shell subprocess, wall-clock timeout, process-group kill.
- `tools/`: 5 of 6 tools working — `list_files`, `read_file`, `write_file`, `git_diff`,
  `run_tests`. Registry enforces timeout + `asyncio.Semaphore` concurrency cap and turns
  every failure into `ToolResult(ok=False)`.
- `llm/`: `LLMClient` Protocol, `AnthropicLLM` (structured output via forced tool use),
  `ScriptedLLM` deterministic double.
- `agent/`: `AgentState` TypedDict with `operator.add` reducers, six nodes, conditional
  retry edge, compiled graph.
- `api/`: `POST /runs` (202 + background task), `GET /runs/{id}`, `GET /runs/{id}/events`
  (SSE), `POST /runs/{id}/cancel`, `GET /health`.
- `evaluation/`: task success, tool selection histogram, retry count, diff validity,
  failure reason.
- 26 passing tests. Full loop verified end-to-end over real HTTP + SSE.

## NOW

- **Handwrite task: `search_code` in `src/repopilot/tools/fs_tools.py`.**
  Contract + 6 red tests already in `tests/test_search_code.py`.
- Read the main call chain in the order listed in `docs/architecture.md`.

## NEXT

1. Handwrite `AgentState` from scratch (delete + retype `state.py`) to internalise
   TypedDict + reducers.
2. asyncio deep dive: write a minimal concurrent tool runtime with `gather`,
   `Semaphore`, `wait_for`, and task cancellation.
3. MCP server exposing `read_file` / `search_code` / `git_diff` / `run_tests`.
4. Docker sandbox replacing `sandbox/local.py` (same signature).
5. OpenTelemetry spans per node and per tool.
6. README, architecture diagram, resume bullet points.

## BLOCKED

- Nothing.

## KNOWN GAPS (be honest about these in interviews)

- Run store is in-memory; a restart loses history. No DB yet.
- Sandbox is a local subprocess, not a container. Isolation is path + timeout, not
  kernel-level. Docker is the next step.
- No auth on the API.
- Evaluation is single-run; there is no benchmark suite of tasks yet.
