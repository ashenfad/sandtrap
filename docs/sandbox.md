# Sandbox Execution

The `sandbox()` factory creates a sandbox for executing Python code under a policy-controlled security model. By default (`isolation="none"`), execution is in-process, lightweight, and shares the host's memory space. For subprocess-backed execution with kernel-level isolation, see [process.md](process.md).

## Creating a sandbox

```python
from sandtrap import Policy, sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)
sb = sandbox(policy)
```

**Parameters:**

- `policy` -- a `Policy` instance controlling what sandboxed code can access.
- `isolation` -- `"none"` (default), `"process"`, or `"kernel"`. See [process.md](process.md).
- `mode` -- `"raw"` (default) returns plain objects for user-defined functions and classes. `"wrapped"` wraps them for pickling instead; it is deprecated and warns. See [serialization.md](serialization.md).
- `filesystem` -- a `FileSystem` implementation for VFS interception (see [filesystem.md](filesystem.md)).
- `snapshot_prints` -- when `True`, deep-copies `print()` arguments at call time and populates `result.prints`. Default `False`. Works with all isolation levels.
- `echo` -- `"none"` (default), `"last"`, or `"all"`. REPL/notebook-style auto-display of bare top-level expressions. See [Expression echo](#expression-echo-repl-style).

`sandbox()` takes five more that only mean something once there is a worker process, and are ignored under `isolation="none"` — so a single call can drive every rung. All are documented in [process.md](process.md#creating-a-process-sandbox):

- `start_method` -- how the worker is created (`None` picks the safest available; `"fork"` is the escape hatch for a policy that can't be serialized).
- `preload_grants` -- import granted modules into the forkserver broker so workers inherit them instead of importing their own copies.
- `rpc_handlers` -- bridge live parent-side objects into the worker.
- `allow_degraded` -- `isolation="kernel"` only; proceed with a warning rather than raising when the platform can't apply kernel restrictions.
- `close_fds` -- neutralize ambient host file descriptors in the worker.

## Context manager

`sandbox()` returns objects that support `with`:

```python
with sandbox(policy, filesystem=fs) as sb:
    result = sb.exec("x = 1")
```

Filesystem and network patches are installed once on first use and remain active for the process lifetime. They are inert when no sandbox is executing -- calls fall through to the original functions transparently.

## Running code

### Synchronous

```python
result = sb.exec("x = 2 + 3")
```

With a pre-populated namespace:

```python
result = sb.exec("y = x + 1", namespace={"x": 10})
```

### Asynchronous

```python
import asyncio

result = asyncio.run(sb.aexec("""
import asyncio
await asyncio.sleep(0.01)
x = 42
"""))
```

### Per-exec modules

`namespace` hands code bare names. `modules` hands it whole modules, for one
call: each entry maps a module name to the attributes that module has.

```python
result = sb.exec("""
import host
from host import db

rows = db.query("select 1")
print(host.VERSION)
""", modules={"host": {"db": db, "VERSION": 3}})
```

`import host`, `from host import db`, and `host.db` all resolve, at the top
level and inside a [VFS module](filesystem.md#vfs-imports) -- workspace code
imported during the call sees the same modules the top-level code does.

The rules:

- **It lasts exactly as long as the call.** The next `exec()` gets whatever
  `modules` *it* passes and nothing else, so a pooled worker never serves one
  call's module to the next, and `import host` with no `modules` is an
  `ImportError`.
- **Every name you put there is readable**, including underscore-prefixed
  ones. The policy's include/exclude filters describe what sandboxed code may
  reach on a module you *granted*; you wrote this mapping out by hand.
- **Sandboxed code cannot write to it.** `host.db = ...`, `host.new = ...`,
  and `del host.db` all raise `AttributeError` -- a writable per-exec module
  would be a channel from one execution to the next.
- **It is import-only.** The module is not bound as a bare name and does not
  come back in `result.namespace`.
- **The name must be free.** A name the policy already grants a module under,
  or `sys`, raises `ValueError` at the call. Names are plain identifiers;
  dotted packages are not supported.

Under `isolation="process"` / `"kernel"` the mapping crosses to the worker with
the namespace, through the same picklability filter: plain data goes by value,
and a live parent-side object goes as an
[`RpcProxyMarker`](serialization.md#cross-process-resources-via-rpc-process--kernel-isolation) that the worker turns into
a proxy onto the real object.

## ExecResult

Both `exec()` and `aexec()` return an `ExecResult`:

| Field | Type | Description |
|-------|------|-------------|
| `namespace` | `dict[str, Any]` | Variables defined by the sandboxed code |
| `stdout` | `str` | Captured `print` + `sys.stdout` output (see below) |
| `stderr` | `str` | Captured `sys.stderr` output (see below) |
| `error` | `BaseException \| None` | Runtime error, or `None` on success |
| `ticks` | `int` | Number of checkpoint ticks consumed |
| `prints` | `list[tuple[Any, ...]]` | Raw `print()` args, deep-copied at call time (empty unless `snapshot_prints=True`) |

The namespace excludes sandbox internals (`__builtins__`, `__st_*` gates, registered functions/classes, `print`). If user code reassigns a registered name (e.g., `print = 42`), the new value is included.

### stdout / stderr capture

`result.stdout` collects everything written to stdout during the execution: sandboxed `print` calls *and* host-side writes to the real `sys.stdout` made by registered library code — `df.info()` is the canonical case (it grabs `sys.stdout` internally, so the injected `print` never sees it). Both routes feed one buffer, so interleaving between `print` and library output is preserved. `result.stderr` is the same story for `sys.stderr`: the synthetic sandbox `sys.stderr` (when `stdin`/`argv` are given) plus host-side writes — `warnings.warn` output, a library's own diagnostics.

Host-side capture works by installing a router over the process's `sys.stdout`/`sys.stderr` (once, idempotent) that delegates to the active execution's buffer via a `ContextVar` and falls through to the real stream otherwise — the same pattern as the global `print` patch. Because routing is per-context rather than a global swap, concurrent executions in one process each get their own stream, and writes outside any execution reach the real streams untouched. The contextvar-propagating threading patches install alongside, so capture follows host libraries into threads they spawn.

Host callbacks invoked from inside an execution inherit its routing — their console output lands in the result. A callback that wants the operator's real console instead (progress logging, sub-agent streaming) opts out per-write:

```python
with sandtrap.passthrough_stdio():
    print("visible on the real console, not in result.stdout")
```

Caveats: code that stored a reference to the real stream *before* the router installed (e.g. a `logging.StreamHandler()` constructed at import time) bypasses capture, same as the `print` patch — and C-level writes straight to the file descriptor never see the router.

## Capturing print objects

`result.stdout` always captures formatted text. When you need the original Python objects passed to `print()`, enable `snapshot_prints`:

```python
with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
    result = sb.exec("""
data = [1, 2, 3]
print("result:", data)
""")

result.stdout    # 'result: [1, 2, 3]\n'
result.prints    # [('result:', [1, 2, 3])]
```

Objects are deep-copied at print time, so mutations after `print()` don't affect `result.prints`. If deep-copy fails (e.g., for objects that don't support it), the raw reference is kept instead.

`snapshot_prints` works with all isolation levels. With process isolation (`isolation="process"` or `"kernel"`), prints are pickled back with the result -- any entries that can't be pickled are silently dropped.

If code errors mid-execution, `result.prints` still contains all prints that occurred before the error.

## Expression echo (REPL-style)

Agents (and people) coming from notebooks often write a bare expression -- `x` -- expecting to see its value, as at a REPL. By default sandtrap runs it silently, like a script. The `echo` option enables notebook semantics:

```python
with sandbox(Policy(timeout=5.0), echo="all") as sb:
    result = sb.exec("""
x = 41 + 1
x
print("mid", x)
'done'
""")

result.stdout    # "42\nmid 42\n'done'\n"
```

- `"none"` (default) -- script semantics, no echo.
- `"all"` -- every bare top-level expression echoes its value.
- `"last"` -- only a *final* expression statement echoes (Jupyter's `last_expr`).

Echo follows `sys.displayhook` conventions:

- **repr, not str** -- `'done'` echoes with quotes; `print` output stays raw.
- **`None` is suppressed** -- `print(x)` is itself a top-level expression whose value is `None`, so it never double-echoes; a call returning a value echoes that value.
- **Top level only** -- expressions inside functions, loops, or `if` blocks never echo.
- A leading string followed by other statements is a module docstring and is not echoed; a program that is *just* a string literal echoes like a notebook cell.

An echoed value lands in **both** output channels at its execution position, exactly as if it had been passed to a single-argument `print`: its `repr` goes to `result.stdout`, and with `snapshot_prints=True` the raw object is appended to `result.prints` as a `(value,)` tuple. Interleaving with real prints is preserved in both channels, and consumers that render `result.prints` downstream (e.g. with a budgeted renderer) handle displays and prints identically:

```python
with sandbox(Policy(timeout=5.0), echo="all", snapshot_prints=True) as sb:
    result = sb.exec("1\nprint('two')\n3")

result.stdout    # '1\ntwo\n3\n'
result.prints    # [(1,), ('two',), (3,)]
```

`echo` works with all isolation levels, and `result.stdout` remains capped by `Policy.max_stdout` -- a huge echoed repr is tail-truncated like any other output. Displayed expressions pass through the same attribute/policy gates as all other code, and each display fires a checkpoint like a `print` call.

### Per-exec override

`exec()`/`aexec()` accept their own `echo=` to override the sandbox's construction-time mode for a single call (`None`, the default, keeps it). One sandbox can serve two surfaces -- a notebook-style caller and a script-semantics caller -- without paying for two sandboxes, which under process isolation would mean two worker processes:

```python
with sandbox(Policy(timeout=5.0)) as sb:      # constructed quiet
    sb.exec("41 + 1", echo="last").stdout     # "42\n"  — REPL surface
    sb.exec("41 + 1").stdout                  # ""      — script surface
```

The override is per-call and crosses the process boundary (the worker applies it for that execution only). Invalid values raise `ValueError` host-side, before any code runs -- on the calling task for `aexec`.

## Error handling

All errors are captured on `result.error` without crashing the host:

```python
result = sandbox.exec("x = 1 / 0")
assert isinstance(result.error, ZeroDivisionError)
```

This includes validation errors (unsupported syntax, reserved names, etc.):

```python
result = sandbox.exec("from os import *")
assert isinstance(result.error, StValidationError)
```

When a validation error occurs, no code executes -- `result.namespace` is empty, `result.stdout` is `""`, and `result.ticks` is `0`.

### Sandbox errors

All sandbox-specific errors inherit from `StError`:

```
StError
├── StValidationError   # invalid AST (before execution)
├── StTimeout           # wall-clock timeout exceeded
├── StTickLimit         # tick limit exceeded
└── StCancelled         # sandbox.cancel() called
```

`MemoryError` (stdlib) is raised when the memory limit is exceeded.

```python
from sandtrap import StError, StValidationError, StTimeout, StTickLimit, StCancelled
```

All errors appear on `result.error`. Check `isinstance(result.error, StValidationError)` to distinguish code that was rejected before execution from code that failed at runtime.

## Cancellation

Cancel a running execution from another thread:

```python
import threading

timer = threading.Timer(1.0, sb.cancel)
timer.start()

result = sb.exec("while True: pass")
assert isinstance(result.error, StCancelled)
```

`cancel()` is safe to call from any thread. The sandbox raises `StCancelled` at the next checkpoint.

The other in-process limits work the same way. `timeout`, `tick_limit`, and `cancel()` are checked only at checkpoints -- loop iterations, function entries, comprehension steps -- so none of them can interrupt a call that is already running. A long C call or a catastrophic regex holds the thread until it returns, and the timeout fires at the next checkpoint after that: in-process it is best-effort between checkpoints, not a deadline. `memory_limit` is the one exception, and only on Linux: there it also installs an `RLIMIT_AS` address-space cap, so the kernel can refuse an allocation and raise `MemoryError` inside a C call without waiting for a checkpoint; on macOS and Windows it is checkpoint-only like the rest (see [Memory limits](security.md#memory-limits)). `isolation="process"` is what can actually kill a runaway worker.

## Reactivation

See [serialization.md](serialization.md) for `sandbox.activate()` and the `__sandtrap_activate__` container hook.
