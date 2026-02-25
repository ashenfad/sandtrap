# Security Model

## How it works

Source code goes through five stages:

1. **Parse** -- `ast.parse()` produces an AST
2. **Validate** -- the rewriter rejects unrecognized AST nodes (fail-closed)
3. **Rewrite** -- attribute access, imports, loops, and function/class definitions are transformed to route through gate functions
4. **Compile** -- the rewritten AST is compiled to bytecode
5. **Execute** -- bytecode runs with restricted `__builtins__` and gate functions in the namespace

## Gate functions

The rewriter injects calls to these internal functions:

| Gate | Purpose |
|------|---------|
| `__st_getattr__` | Policy-checked attribute read (`obj.attr`) |
| `__st_setattr__` | Policy-checked attribute write (`obj.attr = x`) |
| `__st_delattr__` | Policy-checked attribute delete (`del obj.attr`) |
| `__st_import__` | Module import (`import x`) |
| `__st_importfrom__` | From-import (`from x import y`) |
| `__st_checkpoint__` | Timeout, tick limit, memory, and cancellation check |
| `__st_defun__` | Function definition wrapping (wrapped mode) |
| `__st_defclass__` | Class definition wrapping (wrapped mode) |

All `obj.attr` access in sandboxed code -- including in f-strings and augmented assignments -- goes through the getattr gate.

## Builtins whitelist

Sandboxed code gets a restricted `__builtins__` (frozen via `_FrozenBuiltins`, a read-only dict subclass). Access to `__builtins__` itself is blocked at the AST level — sandboxed code cannot reference it. The builtins it contains:

**Available**: `abs`, `all`, `any`, `ascii`, `bin`, `bool`, `bytearray`, `bytes`, `callable`, `chr`, `classmethod`, `complex`, `dict`, `divmod`, `enumerate`, `filter`, `float`, `format`, `frozenset`, `getattr` (policy-gated), `hasattr` (policy-gated), `hash`, `hex`, `id`, `int`, `isinstance`, `issubclass`, `iter`, `len`, `list`, `locals`, `map`, `max`, `min`, `next`, `object`, `oct`, `ord`, `pow`, `property`, `range`, `repr`, `reversed`, `round`, `set`, `slice`, `sorted`, `staticmethod`, `str`, `sum`, `super`, `tuple`, `type` (single-arg only), `zip`, plus ~40 exception types.

**Not available**: `exec`, `eval`, `compile`, `__import__`, `globals`, `vars`, `open` (unless filesystem provided), `dir`, `help`, `breakpoint`, `exit`, `quit`, `input`, `memoryview`.

`getattr()` and `hasattr()` are routed through the attribute policy -- they respect the same allow/deny rules as `obj.attr` syntax.

**Not available as names**: `BaseException`, `KeyboardInterrupt`, `GeneratorExit`, `SystemExit`.

## What's blocked

- **Arbitrary imports** -- only policy-registered modules and VFS files
- **Private attributes** -- `_name` and `__dunder__` (except allowed dunders) blocked by default
- **Network I/O** -- socket operations blocked unless `allow_network=True`
- **File I/O** -- routes through VFS when filesystem provided, otherwise `open` unavailable
- **`type(name, bases, dict)`** -- three-arg form blocked (prevents dynamic class creation outside the rewriter)
- **`str.format` traversal** -- `"{0.__class__}".format(obj)` blocked
- **`__builtins__` access** -- blocked at the AST level; sandboxed code cannot read `__builtins__`
- **`__st_*` names** -- reserved namespace rejected at validation time
- **`globals()`** -- not available
- **Bare `except:`** -- automatically rewritten to `except Exception:` so sandboxed code cannot catch `BaseException` subclasses (`StTimeout`, `StCancelled`, `KeyboardInterrupt`, `SystemExit`). This is a deliberate semantic change: Python's bare `except:` normally catches everything including `BaseException`, but in the sandbox it only catches `Exception` and below

## Checkpoint enforcement

Checkpoints are injected at:
- Start of every loop body (`for`, `while`)
- Start of every function/method body
- Every comprehension iteration (`[x for x in ...]`)
- Every call to a non-type builtin function (`len`, `sorted`, `sum`, etc.)

Type builtins (`str`, `int`, `dict`, `range`, etc.) do not fire checkpoints -- they are real types so that library code receiving them (e.g. `df.astype(str)`) works correctly.

Each checkpoint increments the tick counter and checks: cancellation flag, tick limit, wall-clock timeout, and memory limit (in that order).

## Threat model

sandtrap is a "walled garden" -- it controls what sandboxed code can access, not what the Python runtime can do. It is designed to prevent accidental or casual misuse by LLM-generated code.

It is **not** a security boundary against a determined attacker with full CPython knowledge. It runs in-process and shares the same memory space as the host. If you need hard isolation, use process-level sandboxing (containers, seccomp, etc.) as an outer layer.

### What's defended

- Attribute traversal attacks (MRO walking, `__subclasses__()`, `__globals__`)
- Dynamic class creation via `type(name, bases, dict)`
- Import of unregistered modules
- `str.format` field traversal (`{0.__class__}`)
- Swallowing control exceptions via bare `except:`
- Infinite loops and runaway computation (tick limits, timeouts)
- Unauthorized file and network I/O

### What's out of scope

These vectors are **not** defended against and require process-level isolation if they are a concern:

- **C extensions** -- a registered module with C code can do anything (call `ctypes`, access raw memory, spawn processes). Only register modules you trust.
- **`ctypes` / `cffi`** -- if registered, these provide unrestricted access to the C layer. Never register them.
- **`gc.get_objects()`** -- if the `gc` module is registered, sandboxed code can enumerate all live Python objects. Don't register `gc`.
- **Signal handlers** -- `signal` module access would allow overriding the host's signal handling.
- **Shared mutable state** -- objects passed into the sandbox namespace are not copied. Sandboxed code can mutate them in place.
- **Side channels** -- timing attacks, cache probing, and other side channels are not mitigated.
- **CPython internals** -- bytecode manipulation, `sys._getframe()`, `ctypes.pythonapi`, and other CPython-specific escape hatches are blocked by the attribute gate and builtins whitelist, but novel CPython exploits may bypass AST-level controls.

## Process-global patches

Filesystem interception is provided by [monkeyfs](https://github.com/ashenfad/monkeyfs), which monkey-patches `builtins.open`, `os.stat`, `os.path.exists`, and 20+ other stdlib functions at the process level. Network interception patches `socket.socket.connect` etc. similarly. Patches are installed once on first use and remain active permanently. They dispatch via `ContextVar` -- when no sandbox is executing, all calls fall through to the original functions transparently. This is necessary so that registered libraries (e.g., `pd.read_csv`) see the virtual filesystem during sandbox execution.
