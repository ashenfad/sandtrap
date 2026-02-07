# Policy & Registration

The `Policy` object defines what sandboxed code is allowed to access. The sandbox starts with no access to external libraries, modules, or host functions -- you register exactly what you want to expose.

## Policy options

```python
from sblite import Policy

policy = Policy(
    timeout=30.0,        # wall-clock seconds (default 30, None to disable)
    tick_limit=None,     # max checkpoint ticks (None to disable)
    memory_limit=None,   # MB of additional RSS headroom (None to disable)
    max_stdout=None,     # max chars of captured stdout, keeps tail (None for unlimited)
    allow_network=False, # allow socket operations (default False)
)
```

### timeout vs tick_limit

`timeout` is wall-clock time. It protects the host from runaway sandboxes but penalizes I/O wait time (LLM calls, sub-agent delegation, etc.).

`tick_limit` counts Python-level control flow steps -- each loop iteration, function entry, and comprehension step is one tick. It's deterministic and I/O-agnostic: waiting 30 seconds on a network call costs zero ticks. C-extension work (numpy, pandas, json) also costs zero ticks since it doesn't hit Python checkpoints.

For agent workloads, a generous `timeout` (safety net) plus a tighter `tick_limit` (abuse prevention) is recommended.

## Registering functions

```python
def my_helper(x):
    return x * 2

policy.fn(my_helper)
```

Decorator syntax:

```python
@policy.fn
def another_helper(x):
    return x + 1
```

With options:

```python
@policy.fn(name="fetch", network_access=True)
def fetch_url(url):
    ...
```

**Options**: `name` (override function name), `host_fs_access` (grant real filesystem access), `network_access` (grant network access).

## Registering classes

```python
policy.cls(MyClass)
```

Decorator syntax:

```python
@policy.cls
class MyClass:
    ...
```

With options:

```python
@policy.cls(constructable=False)
class MyClass:
    ...

policy.cls(MyClass, include="get_*")           # only expose get_* methods
policy.cls(MyClass, exclude="_*")              # hide private attrs (default)
policy.cls(MyClass, host_fs_access=True)       # methods get real filesystem access
```

**Options**: `name`, `constructable`, `include`, `exclude`, `configure`, `host_fs_access`, `network_access`.

## Registering modules

```python
import math

policy.module(math)
```

With filtering:

```python
import os.path

policy.module(os.path, include=("join", "basename", "dirname"))
```

Recursive registration exposes submodules:

```python
import json

policy.module(json, recursive=True)
```

Registering a live object as a module:

```python
policy.module(my_service, name="service")
```

**Options**: `name`, `include`, `exclude`, `configure`, `recursive`, `host_fs_access`, `network_access`.

## Pattern filtering

`include` and `exclude` accept:

- A glob string: `"get_*"`
- An iterable of globs: `("_*", "*._*")`
- A callable predicate: `lambda name: name.startswith("safe_")`

Defaults: `include="*"` (everything), `exclude="_*"` (private attributes hidden).

## Per-member overrides

Use `MemberSpec` in the `configure` dict for fine-grained control:

```python
from sblite import MemberSpec

policy.module(
    my_module,
    configure={
        "write_file": MemberSpec(host_fs_access=True),
        "fetch": MemberSpec(network_access=True),
    },
)
```
