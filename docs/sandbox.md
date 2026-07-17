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
- `mode` -- `"wrapped"` (default) wraps user-defined functions/classes for pickling. `"raw"` returns plain objects. See [serialization.md](serialization.md).
- `filesystem` -- a `FileSystem` implementation for VFS interception (see [filesystem.md](filesystem.md)).
- `snapshot_prints` -- when `True`, deep-copies `print()` arguments at call time and populates `result.prints`. Default `False`. Works with all isolation levels.
- `echo` -- `"none"` (default), `"last"`, or `"all"`. REPL/notebook-style auto-display of bare top-level expressions. See [Expression echo](#expression-echo-repl-style).

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

## Reactivation

See [serialization.md](serialization.md) for `sandbox.activate()` and the `__sandtrap_activate__` container hook.
