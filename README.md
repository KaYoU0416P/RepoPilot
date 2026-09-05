# RepoPilot

A controlled repository / coding agent. Give it a task and a repo; it plans a change,
edits files, runs the test suite in an isolated workspace, retries on failure within a
budget, and returns a unified diff plus a machine-readable evaluation of its own run.

Built with FastAPI, LangGraph, Pydantic v2 and asyncio.

```
POST /runs ──▶ analyze ──▶ plan ──▶ execute ──▶ run_tests ──▶ evaluate ──▶ finish ──▶ diff
                                      ▲                          │
                                      └────── retry (budgeted) ──┘
```

## Quick start

```bash
make sync          # uv sync (+ macOS .pth fixup, see docs/failures.md)
make test          # 26 pass, 5 fail on purpose - see "Handwrite task" below
make demo          # run one task end-to-end, no server, no API key
make run           # uvicorn on :8000
```

No API key is required. With `ANTHROPIC_API_KEY` unset, RepoPilot falls back to
`ScriptedLLM`, a deterministic test double that only understands the bundled fixture
repo. Set the key in `.env` (see `.env.example`) to use a real model.

### Drive it over HTTP

```bash
curl -s localhost:8000/health

RID=$(curl -s -X POST localhost:8000/runs \
  -H 'content-type: application/json' \
  -d '{"task":"Fix divide() so dividing by zero raises ValueError"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["run_id"])')

curl -sN localhost:8000/runs/$RID/events     # live SSE, one frame per node
curl -s  localhost:8000/runs/$RID | python3 -m json.tool
```

## What it actually does

The bundled `fixtures/sample_repo` has a real bug: `divide()` has no zero check, so
`test_divide_by_zero_raises_value_error` fails. A run copies that repo into
`.workspaces/<run_id>/`, commits a baseline, lets the agent work, and diffs the result:

```
verdict: success
files changed: calculator.py
tests: passed
attempts: 1
tool calls: 6
```

```diff
 def divide(a: float, b: float) -> float:
+    if b == 0:
+        raise ValueError("division by zero")
     return a / b
```

## Design

The constraint that shapes everything: **LLM-generated code never runs unsandboxed
against the host repository.** Three separate layers of containment:

| Risk | Mitigation |
|---|---|
| Model writes outside the repo | `Workspace.resolve()` rejects absolute paths, `..`, symlink escapes |
| Generated code never terminates | Wall-clock timeout + `os.killpg` on the process group |
| A bad edit corrupts the real repo | Agent operates on a `copytree`, never the original |

There is deliberately **no general shell tool**. The only execute-risk tool is
`run_tests`, which runs a fixed pytest command line.

Every tool call goes through one registry that enforces a timeout, an
`asyncio.Semaphore` concurrency cap, and converts any failure into
`ToolResult(ok=False)` so a bad tool can never kill a run.

Full call chain and module boundaries: **[docs/architecture.md](docs/architecture.md)**.

## Evaluation

A run is not judged only by its final output. `evaluation/metrics.py` reports the
trajectory:

```json
{
  "task_success": true,
  "tests_passed": true,
  "retry_count": 0,
  "tool_calls_total": 7,
  "tool_calls_failed": 1,
  "tool_selection": {"list_files": 1, "read_file": 2, "write_file": 1, "run_tests": 1},
  "diff_valid": true,
  "failure_reason": "none"
}
```

## Handwrite task

`search_code` in [src/repopilot/tools/fs_tools.py](src/repopilot/tools/fs_tools.py) is
intentionally unimplemented. Its contract lives in the docstring and its six tests in
[tests/test_search_code.py](tests/test_search_code.py) are red until you write it.

```bash
uv run pytest tests/test_search_code.py -v
```

The registry catches `NotImplementedError` and degrades it to a failed `ToolResult`, so
the rest of the agent keeps working meanwhile.

## Project layout

```
src/repopilot/
  api/            FastAPI routes, DTOs, SSE, run lifecycle
  agent/          AgentState, nodes, graph wiring, prompts
  tools/          tool contract + registry, fs/git/test tools
  workspace/      per-run repo copy, path confinement
  sandbox/        subprocess execution with hard timeout
  llm/            provider adapters, structured output
  evaluation/     trajectory metrics
  observability/  run_id context, logging
fixtures/sample_repo/   target repo used by the demo and tests
docs/            architecture, progress, learning notes, failure log
```

## Status

Working: the full loop, six tools, SSE streaming, retry budget, evaluation, 26 tests.

Not built yet: MCP server, Docker sandbox, OpenTelemetry traces, persistence, auth.
Honest gaps are tracked in [docs/progress.md](docs/progress.md).
