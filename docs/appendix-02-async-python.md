# Appendix 02 — Async Python from First Principles

Every function in the harness engine is `async`. If you are not comfortable with
`async/await`, reading the engine code feels like reading a foreign language.
This appendix explains it from the ground up, then shows you how to read it in
the context of this codebase.

---

## The problem async solves

Consider calling two APIs sequentially:

```
call Claude API  →  wait 2 seconds  →  result A
call Gemini API  →  wait 1.5 seconds  →  result B

Total wall-clock time: 3.5 seconds
```

Both calls are **I/O-bound** — the CPU is idle while waiting for the network.
You are wasting 3.5 seconds of real time doing nothing.

With async:

```
call Claude API  ─────────────────────────► result A (2s)
call Gemini API  ────────────────────► result B (1.5s)

Total wall-clock time: 2 seconds  (overlapping)
```

Async lets one thread manage many concurrent waits without blocking.

---

## The two words you need: `async def` and `await`

### `async def` — marks a function as a coroutine

```python
# Normal function — runs immediately, returns a value
def get_data():
    return 42

# Async function — returns a coroutine object (a paused computation)
async def get_data():
    return 42
```

When you call an `async def` function, you get back a **coroutine** — not the
result. The coroutine hasn't run yet. It is a description of work to be done.

```python
coro = get_data()   # does NOT run get_data — returns a coroutine object
print(coro)         # <coroutine object get_data at 0x...>
```

To actually run it, you must `await` it.

### `await` — run this coroutine and give me its result

```python
result = await get_data()   # NOW it runs; result = 42
```

`await` can only be used inside another `async def` function. This is why async
tends to spread through a codebase: if you `await` something, your function must
also be `async`.

---

## The event loop — the scheduler behind it all

Python has a built-in scheduler called the **event loop**. It keeps a queue of
coroutines that are ready to run, and switches between them when one is waiting
for I/O.

```
Event Loop:
  ┌────────────────────────────────────────────────┐
  │  Queue: [coroutine_A, coroutine_B, ...]        │
  │                                                │
  │  Run coroutine_A until it hits `await`         │
  │    → A is now waiting for network              │
  │    → Switch to coroutine_B                     │
  │  Run coroutine_B until it hits `await`         │
  │    → B is now waiting for network              │
  │    → Check if A's network call is done         │
  │    → It is! Resume coroutine_A                 │
  │  ... and so on                                 │
  └────────────────────────────────────────────────┘
```

**No threads are created.** This is cooperative multitasking: coroutines
voluntarily yield control at `await` points. One thread, many concurrent waits.

---

## `asyncio.run()` — the entry point

You cannot `await` at the top level of a script. You need to hand a coroutine to
the event loop:

```python
import asyncio

async def main():
    result = await some_async_function()
    print(result)

asyncio.run(main())   # creates the event loop, runs main(), shuts down
```

In the harness CLI:

```python
# harness/cli.py — bottom of file
def main():
    args = build_parser().parse_args()
    asyncio.run(args.fn(args))       # args.fn is cmd_run, cmd_demo, etc.
```

`asyncio.run()` is always at the boundary between synchronous code (CLI, tests)
and the async engine.

---

## Reading async code — a practical guide

### Pattern 1: `await` on a network call

```python
# harness/providers/anthropic_provider.py
resp = await self._client.messages.create(**params)
```

Translation: *"Start this API call. Give up the thread. When the response
arrives (could be 1–5 seconds), resume here and assign to `resp`."*

Nothing special about the logic — it's just an API call that would block if
synchronous. `await` makes it non-blocking.

### Pattern 2: `async def` propagating up the call stack

```python
# engine.py
async def _model_call(self, ...) -> ModelResponse:
    return await self.chain.complete(...)     # chain.complete is async

async def run_agent(self, ...) -> ExecutionResult:
    ...
    response = await self._model_call(...)    # _model_call is async
    ...

async def cmd_run(args):
    ...
    res = await engine.run_agent(...)         # run_agent is async
```

Each function is `async` because it calls something that is `async`. The
`await` chain goes all the way up to `asyncio.run()`.

### Pattern 3: `async for` — async iteration

Not used heavily in this codebase, but worth knowing: some libraries return
async iterators (e.g., streaming responses). You iterate them with `async for`.

```python
async for chunk in stream:
    print(chunk)
```

### Pattern 4: running coroutines concurrently

```python
import asyncio

# Sequential — 3 + 2 = 5 seconds total
result_a = await call_claude()
result_b = await call_gemini()

# Concurrent — max(3, 2) = 3 seconds total
result_a, result_b = await asyncio.gather(call_claude(), call_gemini())
```

`asyncio.gather()` schedules multiple coroutines and waits for all of them.
The harness does not use `gather()` in the main loop (model calls are
sequential — turn 1 must complete before turn 2 starts), but it is important
to know for the worker, which could claim multiple runs concurrently.

---

## Why every harness function is async

Three reasons:

1. **Model API calls are network I/O.** Each `chain.complete()` call waits for
   a remote server. Making this async means the worker can handle other jobs
   while waiting.

2. **Database and S3 calls are also I/O.** `resolve_sources()` queries Postgres;
   `write_targets()` writes to S3. All I/O, all async.

3. **MCP tool calls are network I/O.** The `MCPClient` opens HTTP sessions to
   the MCP server. Async is the only sane way to manage these.

If any of these were synchronous, one slow API response would block the entire
Python process — nothing else could run.

---

## Common confusion: forgetting `await`

```python
# BUG: forgot await — result is a coroutine object, not the ModelResponse
response = self.chain.complete(...)
print(response.stop_reason)    # AttributeError: coroutine has no attribute stop_reason

# CORRECT:
response = await self.chain.complete(...)
print(response.stop_reason)    # works
```

Python will warn you (`RuntimeWarning: coroutine 'complete' was never awaited`)
but it won't raise an error at the call site. The bug surfaces later when you
try to use the result.

---

## How to read the engine loop with this knowledge

Here is the core of `_agent_loop` in `harness/core/engine.py`, annotated:

```python
async def _agent_loop(self, ...):
    while True:
        quota.check_turn()                       # sync — no I/O, just a counter

        response = await self._model_call(...)   # ASYNC: network call to Claude/Gemini
                                                 # thread yields here until response arrives

        if response.stop_reason != StopReason.TOOL_USE:
            break                                # sync: just a comparison

        results = []
        for tc in response.tool_calls:
            rec = await self.tools.call(...)     # ASYNC: tool may be MCP (network) or
                                                 # python (sync, but wrapped as async)
            results.append(ToolResultBlock(...)) # sync

        messages.append(Message.tool_results(results))  # sync: just list append

    tgt_audit = await self.binder.write_targets(...)     # ASYNC: S3 write
    return await self._finish(...)                       # ASYNC: decision log write
```

Every `await` marks a point where the event loop *could* switch to another
coroutine. Everything between `await`s runs synchronously on the thread.

---

## Summary

| Concept | What it means |
|---|---|
| `async def` | Declares a coroutine — a pauseable function |
| `await` | Run this coroutine; yield the thread until it's done |
| Event loop | The scheduler that switches between coroutines at `await` points |
| `asyncio.run()` | The bridge from sync code into the async world |
| Why it matters | Network I/O (APIs, DB, S3, MCP) can overlap instead of waiting in sequence |

When reading the engine: treat every `await` as "this is a network call." The
logic around it is identical to synchronous code.
