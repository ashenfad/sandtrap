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
| `__sb_getattr__` | Policy-checked attribute read (`obj.attr`) |
| `__sb_setattr__` | Policy-checked attribute write (`obj.attr = x`) |
| `__sb_delattr__` | Policy-checked attribute delete (`del obj.attr`) |
| `__sb_import__` | Module import (`import x`) |
| `__sb_importfrom__` | From-import (`from x import y`) |
| `__sb_checkpoint__` | Timeout, tick limit, memory, and cancellation check |
| `__sb_defun__` | Function definition wrapping (task mode) |
| `__sb_defclass__` | Class definition wrapping (task mode) |

All `obj.attr` access in sandboxed code -- including in f-strings and augmented assignments -- goes through the getattr gate.

## Builtins whitelist

Sandboxed code gets a restricted `__builtins__` (frozen via `MappingProxyType`):

**Available**: `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `filter`, `float`, `format`, `frozenset`, `hash`, `hex`, `int`, `isinstance`, `issubclass`, `iter`, `len`, `list`, `map`, `max`, `min`, `next`, `object`, `pow`, `range`, `repr`, `reversed`, `round`, `set`, `slice`, `sorted`, `str`, `sum`, `super`, `tuple`, `type` (single-arg only), `zip`, plus ~40 exception types.

**Not available**: `exec`, `eval`, `compile`, `__import__`, `globals`, `vars`, `open` (unless filesystem provided), `dir`, `help`, `breakpoint`, `exit`, `quit`, `input`, `memoryview`.

**Not available as names**: `BaseException`, `KeyboardInterrupt`, `GeneratorExit`, `SystemExit`.

## What's blocked

- **Arbitrary imports** -- only policy-registered modules and VFS files
- **Private attributes** -- `_name` and `__dunder__` (except allowed dunders) blocked by default
- **Network I/O** -- socket operations blocked unless `allow_network=True`
- **File I/O** -- routes through VFS when filesystem provided, otherwise `open` unavailable
- **`type(name, bases, dict)`** -- three-arg form blocked (prevents dynamic class creation outside the rewriter)
- **`str.format` traversal** -- `"{0.__class__}".format(obj)` blocked
- **`__builtins__` mutation** -- frozen with `MappingProxyType`
- **`__sb_*` names** -- reserved namespace rejected at validation time
- **`globals()`** -- not available

## Checkpoint enforcement

Checkpoints are injected at:
- Start of every loop body (`for`, `while`)
- Start of every function/method body
- Every comprehension iteration (`[x for x in ...]`)

Each checkpoint increments the tick counter and checks: cancellation flag, tick limit, wall-clock timeout, and memory limit (in that order).

## Threat model

sblite is a "walled garden" -- it controls what sandboxed code can access, not what the Python runtime can do. It is designed to prevent accidental or casual misuse by LLM-generated code.

It is **not** a security boundary against a determined attacker with full CPython knowledge. It runs in-process and shares the same memory space as the host. If you need hard isolation, use process-level sandboxing (containers, seccomp, etc.) as an outer layer.
