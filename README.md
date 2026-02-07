# sblite

A lightweight in-process Python sandbox that uses AST rewriting and compiled bytecode execution. Sandboxed code runs at near-native speed while attribute access, imports, and resource usage are controlled by a whitelist-based policy.

## How it works

Source code is parsed into an AST, validated, then rewritten so that:

- **Attribute access** (`obj.attr`) routes through policy-checked gate functions
- **Imports** (`import x`) resolve only policy-registered modules (or VFS modules)
- **Loops and functions** get checkpoint calls for timeout and cancellation enforcement

The rewritten AST is compiled to bytecode and executed with a restricted `__builtins__` set. Network and filesystem access are intercepted via ContextVar-based monkey-patching.

## Quick start

```python
from sblite import Policy, Sandbox

policy = Policy(timeout=5.0)
sandbox = Sandbox(policy)

result = sandbox.exec("""
total = sum(range(10))
print(f"total = {total}")
""")

print(result.stdout)       # "total = 45\n"
print(result.namespace)    # {"total": 45}
print(result.error)        # None
```

## Registering modules, functions, and classes

The sandbox starts with no access to external libraries. You register what you want to expose:

```python
import math

policy = Policy()
policy.module(math)

sandbox = Sandbox(policy)
result = sandbox.exec("""
import math
x = math.sqrt(16)
""")
assert result.namespace["x"] == 4.0
```

### Functions

```python
def my_helper(x):
    return x * 2

policy.fn(my_helper)

# Or with decorator syntax:
@policy.fn
def another_helper(x):
    return x + 1
```

Options: `name`, `host_fs_access`, `network_access`.

### Classes

```python
policy.cls(MyClass)
policy.cls(MyClass, constructable=False)  # visible for isinstance, can't construct
policy.cls(MyClass, include="get_*", exclude="_*")
```

Options: `name`, `constructable`, `include`, `exclude`, `configure`, `host_fs_access`, `network_access`.

### Modules

```python
policy.module(math)
policy.module(os.path, include=("join", "basename", "dirname"))
policy.module(requests, recursive=True, network_access=True)
```

Options: `name`, `include`, `exclude`, `configure`, `recursive`, `host_fs_access`, `network_access`.

### Pattern filtering

`include` and `exclude` accept a glob string (`"get_*"`), an iterable of globs (`("_*", "*._*")`), or a callable predicate (`lambda name: ...`).

## Filesystem interception

Provide a `FileSystem` implementation to intercept all file I/O:

```python
from sblite import MemoryFS, Policy, Sandbox

fs = MemoryFS()
fs.files["/data.txt"] = "hello world"

sandbox = Sandbox(Policy(), filesystem=fs)
result = sandbox.exec("""
f = open('/data.txt')
content = f.read()
f.close()
""")
assert result.namespace["content"] == "hello world"
```

Sandboxed code can also `import` modules from the VFS:

```python
fs.files["/helpers.py"] = "def double(x): return x * 2"

result = sandbox.exec("""
from helpers import double
result = double(5)
""")
assert result.namespace["result"] == 10
```

## Async execution

```python
import asyncio

result = asyncio.run(sandbox.aexec("""
import asyncio
await asyncio.sleep(0.01)
x = 42
"""))
assert result.namespace["x"] == 42
```

## Task mode and pickling

In task mode (the default), functions and classes defined in the sandbox are wrapped in `SbFunction` / `SbClass` / `SbInstance` which support pickling for iterative execution across turns:

```python
import pickle

result = sandbox.exec("def inc(x): return x + 1")
f = result.namespace["inc"]

# Pickle round-trip
data = pickle.dumps(f)
f2 = pickle.loads(data)

# Reactivate with a sandbox
sandbox.activate(f2)
assert f2(5) == 6
```

Set `mode="service"` to get raw functions/classes instead.

## Policy options

```python
Policy(
    timeout=30.0,        # seconds (default 30)
    memory_limit=None,   # MB of additional RSS headroom
    max_stdout=None,     # max chars of stdout (keeps tail)
    allow_network=False, # global network access flag
)
```

## Static analysis

`find_refs` does a conservative static analysis to determine which names a piece of source code reads from the namespace:

```python
from sblite import find_refs

refs = find_refs("y = x + math.sqrt(4)")
# refs == {"x", "math"}
```

## Error handling

Runtime errors are captured on `result.error` without crashing the host:

```python
result = sandbox.exec("x = 1 / 0")
assert isinstance(result.error, ZeroDivisionError)
```

Validation errors (e.g., using `__sb_*` names) raise `SbValidationError` immediately. Timeouts produce `SbTimeout`. Cancellation produces `SbCancelled`.

## Security model

sblite is a "walled garden" — it controls what sandboxed code can access, not what the Python runtime can do. Key properties:

- **Fail-closed rewriter**: unrecognized AST nodes are rejected
- **Attribute gating**: all `obj.attr` access goes through policy checks
- **Import gating**: only registered modules and VFS files are importable
- **No dangerous builtins**: `exec`, `eval`, `compile`, `__import__`, `globals`, `open` (unless filesystem provided), and `vars` are not available
- **No dangerous exceptions**: `BaseException`, `KeyboardInterrupt`, `GeneratorExit`, `SystemExit` are not available as names
- **Checkpoint enforcement**: loops and function calls check for timeout and cancellation
- **Safe `locals()`**: returns a filtered copy excluding sandbox internals
- **Network denial**: socket operations are blocked by default (ContextVar-based)
- **Filesystem interception**: file I/O routes through the provided `FileSystem`

This is not a security boundary against a determined attacker with full CPython knowledge. It's designed to prevent accidental or casual misuse by LLM-generated code.
