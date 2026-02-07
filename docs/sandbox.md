# Sandbox Execution

The `Sandbox` class parses, validates, rewrites, compiles, and executes Python code under a policy-controlled security model.

## Creating a sandbox

```python
from sblite import Policy, Sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)
sandbox = Sandbox(policy)
```

Options:
- `mode` -- `"task"` (default) wraps user-defined functions/classes for pickling. `"service"` returns raw objects.
- `filesystem` -- a `FileSystem` implementation for VFS interception (see [filesystem.md](filesystem.md)).

## Context manager

`Sandbox` supports `with` for automatic cleanup of filesystem and network patches:

```python
with Sandbox(policy, filesystem=fs) as sandbox:
    result = sandbox.exec("x = 1")
# patches are cleaned up here
```

Patches are ref-counted, so overlapping sandboxes work correctly -- patches are only restored when the last sandbox exits.

Without `with`, patches remain installed (harmless -- they're inert when no sandbox is executing).

## Running code

### Synchronous

```python
result = sandbox.exec("x = 2 + 3")
```

With a pre-populated namespace:

```python
result = sandbox.exec("y = x + 1", namespace={"x": 10})
```

### Asynchronous

```python
import asyncio

result = asyncio.run(sandbox.aexec("""
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
| `stdout` | `str` | Captured print output |
| `error` | `BaseException \| None` | Runtime error, or `None` on success |
| `ticks` | `int` | Number of checkpoint ticks consumed |

The namespace excludes sandbox internals (`__builtins__`, `__sb_*` gates, registered functions/classes, `print`). If user code reassigns a registered name (e.g., `print = 42`), the new value is included.

## Error handling

**Runtime errors** are captured on `result.error` without crashing the host:

```python
result = sandbox.exec("x = 1 / 0")
assert isinstance(result.error, ZeroDivisionError)
```

**Validation errors** (e.g., using `__sb_*` names, unsupported syntax) raise `SbValidationError` immediately -- they are not captured on `result.error`.

### Sandbox errors

All sandbox-specific errors inherit from `SbError`:

```
SbError
├── SbValidationError   # invalid AST (raised before execution)
├── SbTimeout           # wall-clock timeout exceeded
├── SbTickLimit         # tick limit exceeded
└── SbCancelled         # sandbox.cancel() called
```

`MemoryError` (stdlib) is raised when the memory limit is exceeded.

```python
from sblite import SbError, SbValidationError, SbTimeout, SbTickLimit, SbCancelled
```

Catching `SbError` catches all sandbox-specific errors. Resource limit errors (`SbTimeout`, `SbTickLimit`, `SbCancelled`, `MemoryError`) appear on `result.error`. `SbValidationError` is raised directly since it occurs before execution begins.

## Cancellation

Cancel a running execution from another thread:

```python
import threading

timer = threading.Timer(1.0, sandbox.cancel)
timer.start()

result = sandbox.exec("while True: pass")
assert isinstance(result.error, SbCancelled)
```

`cancel()` is safe to call from any thread. The sandbox raises `SbCancelled` at the next checkpoint.

## Reactivation

See [task-mode.md](task-mode.md) for `sandbox.activate()`.

## Static analysis

`find_refs` does a conservative static analysis to determine which names a piece of source code reads from the namespace:

```python
from sblite import find_refs

refs = find_refs("y = x + math.sqrt(4)")
# refs == {"x", "math"}
```

This enables lazy deserialization -- only load the state entries the code actually needs.

Pass a `namespace` (any `Mapping`) to follow transitive dependencies through `SbFunction.global_refs`:

```python
refs = find_refs("result = process(data)", namespace=state)
# Discovers process + all SbFunction deps process references
```

The namespace can be a lazy container that deserializes on `get()` -- only values in the dependency chain are touched.

See [task-mode.md](task-mode.md) for details.
