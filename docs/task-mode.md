# Task Mode & Pickling

## Task mode vs service mode

sblite has two execution modes:

- **Task mode** (`mode="task"`, default) -- functions and classes defined in the sandbox are wrapped in `SbFunction` / `SbClass` / `SbInstance`. These wrappers support pickling, enabling iterative execution across turns (e.g., an LLM agent that defines functions in one turn and calls them in the next).

- **Service mode** (`mode="service"`) -- raw Python functions and classes are returned. Simpler, but not serializable.

```python
sandbox = Sandbox(policy, mode="task")     # default, pickleable wrappers
sandbox = Sandbox(policy, mode="service")  # raw objects
```

## Pickle round-trip

```python
import pickle

result = sandbox.exec("def inc(x): return x + 1")
f = result.namespace["inc"]  # SbFunction

# Serialize
data = pickle.dumps(f)

# Deserialize
f2 = pickle.loads(data)

# Reactivate with a sandbox
sandbox.activate(f2)
assert f2(5) == 6
```

## Wrapper types

### SbFunction

Wraps a sandbox-defined function. Stores the rewritten AST so it can be recompiled after deserialization.

```python
f = result.namespace["my_func"]
f(args)           # call it
pickle.dumps(f)   # serialize it
```

### SbClass

Wraps a sandbox-defined class. Supports construction, class attribute access, and `isinstance` checks.

```python
Cls = result.namespace["MyClass"]
obj = Cls(args)   # construct
Cls.class_var     # class attribute access
```

### SbInstance

Wraps an instance of a sandbox-defined class. Proxies attribute access and supports protocol dunders (`__len__`, `__iter__`, `__add__`, etc.).

## Activating deserialized objects

After unpickling, wrappers are "inactive" -- they hold the AST but have no compiled code. Call `sandbox.activate()` to recompile them:

```python
sandbox.activate(f2)                          # SbFunction
sandbox.activate(cls2)                        # SbClass
sandbox.activate(instance2)                   # SbInstance (class must be active first)
sandbox.activate(f2, namespace={"x": state})  # with extra namespace
```

## Self-contained functions

When an `SbFunction` is pickled, it captures its global dependencies (other `SbFunction`/`SbClass` values) as frozen globals. This makes pickled functions self-contained -- they work without manually providing every transitive dependency.

This includes functions imported from VFS modules, which are automatically wrapped as `SbFunction` in task mode.

```python
# sum_squares references square -- frozen automatically on pickle
data = pickle.dumps(sum_squares)
restored = pickle.loads(data)

# Activate without providing square -- frozen globals make it work
sandbox.activate(restored)
restored([1, 2, 3])  # works
```

Frozen globals are a fallback. Namespace values take priority (preserving late-binding semantics):

```python
# Namespace overrides frozen globals
sandbox.activate(restored, namespace={"square": new_square})
restored([1, 2, 3])  # uses new_square
```

**Limitation**: Policy-registered host functions (`policy.fn()`, `policy.module()`) are not `SbFunction` and are not frozen on pickle. They're injected automatically during `exec()` but must be provided explicitly for standalone direct calls after pickle.

## find_refs for lazy deserialization

When you have a large serialized state and only want to deserialize what the next code snippet needs:

```python
from sblite import find_refs

source = "y = x + helper(z)"
refs = find_refs(source)
# refs == {"x", "helper", "z"}

# Only deserialize these keys from your state store
namespace = {k: deserialize(state[k]) for k in refs if k in state}
result = sandbox.exec(source, namespace=namespace)
```

`find_refs` does conservative static analysis -- it may over-report (safe) but won't under-report.

### Transitive dependencies

Pass any `Mapping` as `namespace` to follow `SbFunction` dependencies transitively:

```python
refs = find_refs("result = sum_squares([1, 2, 3])", namespace=state)
# refs == {"sum_squares", "square"}
# square was discovered via sum_squares.global_refs
```

This discovers all names needed, including indirect dependencies through `SbFunction.global_refs`. The namespace can be a lazy container (e.g., one that deserializes on `get()`) -- only values in the dependency chain are accessed.
