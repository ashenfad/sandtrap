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
| `stdout` | `str` | Captured print output (formatted text) |
| `error` | `BaseException \| None` | Runtime error, or `None` on success |
| `ticks` | `int` | Number of checkpoint ticks consumed |
| `prints` | `list[tuple[Any, ...]]` | Raw `print()` args, deep-copied at call time (empty unless `snapshot_prints=True`) |

The namespace excludes sandbox internals (`__builtins__`, `__st_*` gates, registered functions/classes, `print`). If user code reassigns a registered name (e.g., `print = 42`), the new value is included.

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

See [serialization.md](serialization.md) for `sandbox.activate()`.

## Static analysis

`find_refs` does a conservative static analysis to determine which names a piece of source code reads from the namespace:

```python
from sandtrap import find_refs

refs = find_refs("y = x + math.sqrt(4)")
# refs == {"x", "math"}
```

This enables lazy deserialization -- only load the state entries the code actually needs.

Pass a `namespace` (any `Mapping`) to follow transitive dependencies through `StFunction.global_refs`:

```python
refs = find_refs("result = process(data)", namespace=state)
# Discovers process + all StFunction deps process references
```

The namespace can be a lazy container that deserializes on `get()` -- only values in the dependency chain are touched.

See [serialization.md](serialization.md) for details.
