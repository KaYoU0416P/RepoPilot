# Interview-worthy notes

Only things worth saying out loud in an interview. Add as you go.

---

## `asyncio.Semaphore`

**What.** A permit counter. `async with sem:` takes a permit, blocks when none are left,
releases on exit.

**Why here.** `plan` fires every `read_file` and `search_code` concurrently with
`asyncio.gather`. Without a cap, an LLM that names 40 files opens 40 file handles /
subprocesses at once. The semaphore lives in `ToolRegistry`, so *every* tool inherits the
cap and no individual tool has to think about it.

**Java.** `java.util.concurrent.Semaphore`, same idea. Difference: this one blocks a
coroutine, not an OS thread, so a waiting task costs ~nothing.

**One-liner.** "Concurrency is capped in the tool registry, not per call site, so adding
a tool can't accidentally widen the blast radius."

---

## `asyncio.gather` vs threads

**What.** `gather` schedules N coroutines on one event loop thread and waits for all.

**Why here.** Tool work is I/O-bound: file reads, subprocesses, LLM HTTP calls. The loop
switches at every `await`, so N reads overlap on one thread.

**Java.** Closest is `CompletableFuture.allOf`. But Java's default is a thread pool;
asyncio is one thread with cooperative switching — cheaper, and no locking needed for
plain state mutation between awaits.

**One-liner.** "Agent tools are I/O-bound, so concurrency beats parallelism; one event
loop gets the throughput without thread-pool overhead."

**Trap to mention.** A blocking call (`requests.get`, `time.sleep`, big CPU loop) inside
a coroutine freezes the *whole* loop, including unrelated runs. That is why
`sandbox/local.py` uses `asyncio.create_subprocess_exec`, not `subprocess.run`.

---

## `asyncio.wait_for` and timeouts

**What.** Wraps an awaitable; on expiry it **cancels** the inner task and raises
`TimeoutError`.

**Why here.** `ToolRegistry.call` wraps every tool. A hung tool can't stall the graph.

**Java.** `Future.get(timeout)` — but that only stops *waiting*, the work keeps running.
`wait_for` actually cancels the coroutine. Closer to `future.cancel(true)`.

**Trap.** Cancelling a coroutine does **not** kill a child process it spawned. That is
why `run_command` sets `start_new_session=True` and calls `os.killpg` on timeout —
otherwise pytest's children survive as orphans holding the workspace open.

---

## `ContextVar`

**What.** Per-task storage. Each asyncio Task inherits a copy of the current context.

**Why here.** `run_id_var` is set once in `RunService._execute` and every log line from
every node and tool picks it up, with no plumbing through function signatures.

**Java.** `ThreadLocal`, or `MDC` in SLF4J — this is exactly the MDC pattern. Difference:
`ThreadLocal` breaks across a thread pool hand-off; `ContextVar` is copied per Task, so
it survives `create_task` and `gather`.

---

## LangGraph state and reducers

**What.** State is a `TypedDict`. A node returns a **partial** dict; LangGraph merges it.
A key annotated `Annotated[list[X], operator.add]` gets *combined* instead of overwritten.

**Why here.** `tool_calls`, `errors` and `step_log` accumulate across three retry passes
through `execute`. Without reducers each node would have to read the old list, copy it,
append, and write it back — and two concurrent nodes would clobber each other.

**Java.** No direct analogue. Closest mental model: a reducer is `Collectors.reducing`
applied per-key on every state merge, or an event-sourced fold.

**One-liner.** "Nodes return diffs, not the whole state; the reducer decides merge
semantics per key."

---

## Why a graph instead of a `while` loop

The retry path, the budget check and the exit condition are **declared as edges**, so
they are data: inspectable, streamable, and testable in isolation
(`test_conditional_edge_routes_on_verdict` calls the router as a plain function).
A `while` loop hides the same logic inside control flow you can only test by running the
whole thing. It also gives streaming and checkpointing for free.

**Honest caveat.** For a 6-node linear-plus-one-retry flow, a `while` loop would work.
The payoff arrives when you add branches (human approval, multiple executors).

---

## Structured output via forced tool use

**What.** Instead of asking for JSON in prose and parsing it, declare a tool whose
`input_schema` is your Pydantic model's JSON schema and set
`tool_choice={"type": "tool", "name": ...}`. The model must emit a conforming object.

**Why here.** Nodes never string-parse. `Analysis`, `Plan` and `EditSet` arrive as
validated Pydantic instances, and a schema violation is a typed `LLMError` at the
boundary rather than an `AttributeError` three nodes later.

**Java.** Same role as Spring AI's `BeanOutputConverter`, but validated server-side.

---

## Pydantic v2 `BaseModel` vs `TypedDict`

- `BaseModel` — runtime **validation** and coercion. Cost: object construction.
  Used at trust boundaries: HTTP bodies, LLM output, tool results.
- `TypedDict` — a **type-checker-only** annotation on a plain `dict`. Zero runtime cost,
  zero validation. Used for `AgentState`, because LangGraph merges it as a dict.

**Java.** `BaseModel` ≈ a DTO with Bean Validation annotations actually enforced.
`TypedDict` ≈ a `Map<String,Object>` that only the compiler pretends is typed.

**One-liner.** "Validate at the boundary, stay cheap inside."

---

## Why a coding agent needs a sandbox

Three distinct risks, three distinct mitigations:

1. **Wrong path** — model writes `../../.ssh/authorized_keys`. → `Workspace.resolve()`.
2. **Runaway process** — generated test loops forever. → wall-clock timeout + `killpg`.
3. **Blast radius** — a bad edit corrupts the real repo. → operate on a `copytree`.

"Sandbox" is not one feature; naming the three separately is the good answer.
Also worth saying: there is **no general shell tool**, because a shell tool makes every
other restriction decorative.
