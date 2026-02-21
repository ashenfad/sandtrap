# Serialization

In wrapped mode (the default), everything your sandbox code defines is serializable. Define a function in one turn, pickle it, restore it in the next.

```python
import pickle
from sandtrap import Policy, Sandbox

sandbox = Sandbox(Policy())

# Turn 1: define
result = sandbox.exec("def inc(x): return x + 1")
data = pickle.dumps(result.namespace["inc"])

# Turn 2: restore and use
restored = pickle.loads(data)
result = sandbox.exec("y = inc(5)", namespace={"inc": restored})
assert result.namespace["y"] == 6
```

Inactive objects passed via `namespace` are auto-activated -- no manual step needed. Dependencies (other functions, classes) are captured automatically and come along for the ride.

## Wrapped vs raw mode

```python
sandbox = Sandbox(policy)                  # wrapped mode (default)
sandbox = Sandbox(policy, mode="raw")      # raw mode
```

**Wrapped mode** wraps sandbox-defined functions, classes, and instances in serializable containers (`StFunction`, `StClass`, `StInstance`). These store the rewritten AST so they can be pickled and recompiled later.

**Raw mode** returns plain Python objects. Use this when you don't need serialization.

## What gets wrapped

Functions become `StFunction`, classes become `StClass`, and class instances become `StInstance`. All three are callable/usable like their plain equivalents and support `pickle.dumps` / `pickle.loads`. `StInstance` proxies attribute access and forwards protocol dunders (`__len__`, `__iter__`, `__add__`, etc.).

## Activation

After unpickling, wrappers are "inactive" -- they hold the AST but have no compiled code. There are two ways to activate them:

**Auto-activation** (typical workflow): pass inactive objects in `namespace` and they activate automatically:

```python
restored = pickle.loads(data)
result = sandbox.exec("y = f(5)", namespace={"f": restored})
```

**Manual activation** (standalone direct calls from host code):

```python
restored = pickle.loads(data)
sandbox.activate(restored)
restored(5)  # call directly, not via sandbox.exec
```

## Dependencies come along

When a function is pickled, its dependencies on other sandbox-defined functions and classes are captured automatically. This works transitively -- if `a` calls `b` which calls `c`, pickling `a` alone is enough.

```python
result = sandbox.exec("""
def square(x): return x * x
def sum_squares(lst): return sum(square(x) for x in lst)
""")

# Pickle only sum_squares -- square is captured automatically
data = pickle.dumps(result.namespace["sum_squares"])
restored = pickle.loads(data)

sandbox.activate(restored)
restored([1, 2, 3])  # 14 -- works without providing square
```

Namespace values override captured dependencies (preserving late-binding):

```python
sandbox.activate(restored, namespace={"square": different_square})
```

This also works for functions imported from VFS modules -- they're wrapped in the same way and captured as dependencies.

## Selective restore with find_refs

When you have a large serialized state, `find_refs` tells you which names a code snippet needs:

```python
from sandtrap import find_refs

refs = find_refs("y = process(data)")
# refs == {"process", "data"}
```

Pass a namespace to follow transitive dependencies through `StFunction.global_refs`:

```python
refs = find_refs("result = sum_squares([1, 2, 3])", namespace=state)
# refs == {"sum_squares", "square"} -- square discovered transitively
```

The namespace can be any `Mapping`, including lazy containers that deserialize on `get()` -- only values in the dependency chain are accessed.

## Known limitations

- **Policy-hosted functions** (`policy.fn()`, `policy.module()`) are real Python functions, not `StFunction`. They're injected automatically during `exec()` but must be provided explicitly for standalone direct calls after pickle.
- **Classes with `__slots__`** are not supported for pickle round-trips (instance serialization assumes `__dict__`).
- **Class-level mutable state** (e.g., `data = []` at class scope) is not preserved across pickle -- it lives on the class object, which is recompiled from AST.
