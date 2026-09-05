# RepoPilot Architecture

## What it is

A controlled coding agent. You give it a task in natural language and a repository;
it plans a change, edits files, runs the test suite in an isolated workspace, retries
on failure within a budget, and returns a diff plus a machine-readable evaluation.

The design constraint that shapes everything: **LLM-generated code must never run
unsandboxed against the host repository.**

## Main call chain

```
POST /runs                      api/routes.py::create_run
  -> RunService.start           api/service.py       asyncio.create_task, returns 202
       -> WorkspaceManager.create   workspace/manager.py   copytree + git baseline commit
       -> build_graph               agent/graph.py
       -> graph.astream(state)      LangGraph
            analyze    -> list_files, LLM -> Analysis
            plan       -> read_file/search_code (asyncio.gather), LLM -> Plan
            execute    -> read_file, LLM -> EditSet, write_file
            run_tests  -> sandbox subprocess with timeout
            evaluate   -> sets verdict: success | retry | failed
              conditional edge: retry -> execute, else -> finish
            finish     -> git_diff, final report
       -> evaluate_run           evaluation/metrics.py
  -> events pushed to per-run asyncio.Queue
GET /runs/{id}/events           SSE, one `data:` frame per node completion
GET /runs/{id}                  final RunResponse with diff + evaluation
```

## Module responsibilities

| Module | Owns | Does not know about |
|---|---|---|
| `api/` | HTTP, DTOs, SSE, run lifecycle | LangGraph internals, prompts |
| `agent/` | State, nodes, edges, prompts | HTTP, subprocesses |
| `tools/` | Tool contract, registry, timeout + concurrency caps | The graph, the LLM |
| `workspace/` | Per-run repo copy, path confinement | Tools, agent |
| `sandbox/` | Process execution with hard timeout | What is being run |
| `llm/` | Provider adapters, structured output | Tools, workspace |
| `evaluation/` | Trajectory metrics | HTTP, LLM |
| `observability/` | run_id context, log format | everything else |

Dependencies point one way: `api -> agent -> tools -> {workspace, sandbox}`.
`llm` and `evaluation` are leaves.

## Three layers of containment

1. **Workspace** — the agent operates on a `copytree` of the repo, never the original.
2. **Path confinement** — every LLM-supplied path goes through `Workspace.resolve()`,
   which rejects absolute paths, `..` traversal, and symlink escapes. Violations become
   `ToolResult(ok=False)`, not exceptions that kill the run.
3. **Process sandbox** — `sandbox/local.py` runs commands with no shell, a fixed cwd,
   a wall-clock timeout, and `start_new_session=True` so a timeout kills the whole
   process group rather than orphaning children.

There is deliberately **no general `shell` tool**. The only execute-risk tool is
`run_tests`, which runs a fixed pytest command line.

## Budgets

| Budget | Where | Default |
|---|---|---|
| Retries | `evaluate` node vs `max_retries` | 2 |
| Tool timeout | `ToolRegistry.call` -> `asyncio.wait_for` | 20s |
| Test timeout | `sandbox.run_command` | 60s |
| Concurrent tools | `ToolRegistry._semaphore` | 4 |
| Files per edit | `execute` node slice | 5 |
| File read size | `read_file` vs `max_file_bytes` | 200KB |

## LLM providers

`llm/build_llm()` returns either `AnthropicLLM` (structured output via forced tool use)
or `ScriptedLLM`. **`ScriptedLLM` is a deterministic test double, not an agent** — it has
a hardcoded rule table that only understands the bundled fixture repo. It exists so the
whole graph can be tested offline with no tokens. If `ANTHROPIC_API_KEY` is unset,
`get_settings()` falls back to it automatically.

## Current status

Stage 1 (done): local workspace sandbox, in-memory run store, six tools, six nodes.

Not built yet: MCP server, Docker sandbox, OpenTelemetry traces, persistence.
See `docs/progress.md`.
