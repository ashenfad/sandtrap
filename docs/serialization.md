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

`exec()` always returns active wrappers, regardless of isolation level. In-process sandboxes return them active naturally; process/kernel-isolated sandboxes automatically reactivate them before returning the `ExecResult`.

After a manual pickle round-trip (e.g., persisting to a database between turns), wrappers are "inactive" -- they hold the AST but have no compiled code. Pass them back via `namespace` and they auto-activate:

```python
restored = pickle.loads(data)
result = sandbox.exec("y = f(5)", namespace={"f": restored})
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
result = sandbox.exec("y = sum_squares([1, 2, 3])", namespace={"sum_squares": restored})
assert result.namespace["y"] == 14  # works without providing square
```

This also works for functions imported from VFS modules -- they're wrapped in the same way and captured as dependencies.

## Container activation hook

`Sandbox.exec()` auto-activates inactive wrappers (`StFunction`, `StClass`, `StInstance`) sitting at the top level of `namespace`. If you keep wrappers one level deeper -- in a custom dict-like, a registered store, an LRU cache -- they would otherwise stay inactive and raise on call. Host-side containers can opt in by exposing `__sandtrap_activate__`:

```python
class Bag:
    def __init__(self, contents):
        self.contents = contents

    def __sandtrap_activate__(self, activate_value, gates, sandbox, namespace):
        for v in self.contents.values():
            activate_value(v, gates, sandbox=sandbox, namespace=namespace)
```

Sandbox iterates the top-level namespace and calls `v.__sandtrap_activate__(activate_value, gates, sandbox, namespace)` on any value that exposes the method. The `namespace` argument is the top-level dict; passing it through to `activate_value` lets nested wrappers resolve late-bound globals. Hook exceptions are swallowed so a misbehaving container can't break `exec()`.

The hook is **not** invoked on `StFunction` / `StClass` / `StInstance` / `ModuleRef`. Sandbox-defined wrappers are untrusted: the hook body would run with the live `gates` dict in scope and could clear or replace gates to bypass policy on later operations. Only host-side containers defined by the embedder may opt in.

This is the protocol agex's `Cache` uses to keep cached sandbox-defined helpers callable across tasks.

## Cross-process resources via RPC (process / kernel isolation)

`__sandtrap_activate__` covers the in-process case.  Under process or kernel isolation the namespace is pickled into a subprocess, so any container that holds host-side state (database connections, kvgit-backed caches with threading locks, file handles) can't cross the boundary intact.  Sandtrap provides a worker-to-parent RPC channel for these cases.

Register a handler with the sandbox and inject a placeholder marker into the namespace:

```python
from sandtrap import sandbox, Policy, RpcProxyMarker

# Host-side state that the agent should be able to read/write
store = {}

def store_handler(method, args, kwargs):
    if method == "get":
        return store.get(args[0])
    if method == "set":
        store[args[0]] = args[1]
        return None
    raise AttributeError(method)

with sandbox(
    Policy(timeout=5.0),
    isolation="process",
    rpc_handlers={"kv": store_handler},
) as sb:
    result = sb.exec(
        "kv.set('hello', 'world')\n"
        "got = kv.get('hello')\n",
        namespace={"kv": RpcProxyMarker(target="kv")},
    )
    assert result.namespace["got"] == "world"
    assert store == {"hello": "world"}
```

The worker substitutes the marker with an `RpcProxy` bound to its connection.  Each method call on the proxy sends an `RpcCallMsg` to the parent, the parent dispatches to the registered handler, and the return value (or exception) is shipped back as `RpcReturnMsg`.  Calls block synchronously — the worker is single-threaded so only one RPC is outstanding at a time.

For typed wrappers, set `marker.wrapper="module:Class"`:

```python
namespace={"cache": RpcProxyMarker(target="cache", wrapper="agex.cache:RemoteCache")}
```

The worker imports the named class on receipt and instantiates `Class(proxy, *marker.init_args)`.  Resolution failures fall back to the bare `RpcProxy` so the agent gets *something* callable rather than a hard worker-crash.

Limitations to keep in mind:
- The proxy is bound to its worker's connection and can't be pickled — `RpcProxy.__reduce__` raises so it's dropped from result namespaces cleanly.
- Args and return values must pickle.  Things that don't (file handles, thread locks, etc.) need the handler to translate them into something serializable on its own.
- Each call is one IPC round-trip; not free, but order-of-microseconds for small payloads.  Hot loops over thousands of calls warrant a batched method on the handler.
- Re-entrancy isn't supported — a handler must not itself trigger an RPC back into the worker (would deadlock the single-reader loop).

## Known limitations

- **Policy-hosted functions** (`policy.fn()`, `policy.module()`) are real Python functions, not `StFunction`. They're injected automatically during `exec()` but must be provided explicitly via `namespace` when restoring from pickle.
- **Classes with `__slots__`** are not supported for pickle round-trips (instance serialization assumes `__dict__`).
- **Class-level mutable state** (e.g., `data = []` at class scope) is not preserved across pickle -- it lives on the class object, which is recompiled from AST.
