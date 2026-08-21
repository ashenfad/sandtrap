# Policy & Registration

The `Policy` object defines what sandboxed code is allowed to access. The sandbox starts with no access to external libraries, modules, or host functions -- you register exactly what you want to expose.

## Policy options

```python
from sandtrap import Policy

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

Register functions that can be looked up by name — module-level functions,
builtins, classmethods, `functools.partial` of any of those. A **bound method or
callable instance** is deprecated here (it carries the object with it, and
crosses to a worker as a copy); see
[Exposing a live host object](#exposing-a-live-host-object).

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

**Options**: `name`, `include`, `exclude`, `configure`, `recursive`, `host_fs_access`, `network_access`.

## Exposing a live host object

Register the object's **class**, and bind the instance in the exec namespace:

```python
policy.cls(Service, include=("query", "describe"))

with sandbox(policy) as sb:
    sb.exec("rows = service.query('select 1')", namespace={"service": my_service})
```

The class registration carries the policy — member filters, and per-member
privileges via `configure` — while the instance itself never enters the policy.
Both halves matter:

- **The class crosses by name.** Classes pickle by reference, so the policy
  stays portable to a worker that wasn't forked from this process
  (`isolation="process"` / `"kernel"`; see
  [Process Sandbox](process.md) and [the forkserver design notes](forkserver-design.md)).
- **The instance crosses per call**, as a namespace value in-process, or as an
  RPC marker under process isolation — so the live object stays where it is.

A **narrowing wrapper** is the alternative: hand the sandbox a small class that
exposes only what you want, and skip the filters. That works, with one thing it
cannot do — a wrapper cannot grant itself network or host-filesystem access.
Privilege elevation comes from the policy registration and nowhere else, so a
method that has to make a real network call on the sandbox's behalf needs
`network_access=True` on the registration (or a `MemberSpec`), whatever the
object's shape.

Two limits worth knowing. Filters apply to the **bound object**, not to whatever
its methods return — a returned object gets the default rules unless its own
class is registered. And under process/kernel isolation the RPC bridge carries
**method calls only**, so an attribute grant like `include=("token",)` resolves
in-process but has nothing to deliver across a worker boundary.

> **Deprecated:** `policy.module(my_service, name="service")` registered a live
> object directly. It still works and now warns; it will be removed in 0.4.0.
> Under `isolation="process"`/`"kernel"` it never did what it looked like —
> fork handed the worker a **copy**, so mutations never reached the host object
> — and it makes the policy unpicklable, which blocks non-forked workers
> entirely. Migrate to the class registration above. Note that `import service`
> stops working for a live object: bind the name in the namespace instead.
> The same applies to `policy.fn(my_service.query)` — register the class and
> bind the instance rather than registering a bound method.

## Pattern filtering

`include` and `exclude` accept:

- A glob string: `"get_*"`
- An iterable of globs: `("_*", "*._*")`
- A callable predicate: `lambda name: name.startswith("safe_")`

Defaults: `include="*"` (everything), `exclude="_*"` (private attributes hidden).

Patterns **without** a dot match the bare member name. Patterns
**with** a dot match owner-qualified names:

- `"DataFrame.eval"` — class-qualified; checked against every class in
  the object's MRO, so a pattern naming a base class covers subclasses.
- `"numpy.random.seed"`, `"pandas.core*"` — module-path-qualified;
  also applied to `import pandas.core.frame` under a recursive
  registration.

Callable predicates receive bare member names only.

Filters on a `recursive=True` registration apply to its submodules
too — `policy.module(numpy, recursive=True, exclude=("_*", "*._*",
"numpy.random.seed"))` blocks `numpy.random.seed` by attribute access
and by `from numpy.random import seed` alike. Bare excludes gate the
terminal segment of submodule imports as well (the default `"_*"`
blocks `import numpy._core`).

## Per-member overrides

Use `MemberSpec` in the `configure` dict for fine-grained control:

```python
from sandtrap import MemberSpec

policy.module(
    my_module,
    configure={
        "write_file": MemberSpec(host_fs_access=True),
        "fetch": MemberSpec(network_access=True),
    },
)
```
