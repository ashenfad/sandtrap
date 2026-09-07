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
| `__st_dynimport__` | Dynamic import (`__import__(name)`) |
| `__st_checkpoint__` | Timeout, tick limit, memory, and cancellation check |
| `__st_defun__` | Function definition wrapping (wrapped mode) |
| `__st_defclass__` | Class definition wrapping (wrapped mode) |

All `obj.attr` access in sandboxed code -- including in f-strings and augmented assignments -- goes through the getattr gate.

## Builtins whitelist

Sandboxed code gets a restricted `__builtins__` (frozen via `_FrozenBuiltins`, a read-only dict subclass). Access to `__builtins__` itself is blocked at the AST level — sandboxed code cannot reference it. The builtins it contains:

**Available**: `abs`, `all`, `any`, `ascii`, `bin`, `bool`, `bytearray`, `bytes`, `callable`, `chr`, `classmethod`, `complex`, `dict`, `divmod`, `enumerate`, `filter`, `float`, `format`, `frozenset`, `getattr` (policy-gated), `hasattr` (policy-gated), `hash`, `hex`, `id`, `int`, `isinstance`, `issubclass`, `iter`, `len`, `list`, `locals`, `map`, `max`, `min`, `next`, `object`, `oct`, `ord`, `pow`, `property`, `range`, `repr`, `reversed`, `round`, `set`, `slice`, `sorted`, `staticmethod`, `str`, `sum`, `super`, `tuple`, `type` (single-arg only), `zip`, plus ~40 exception types.

**Not available**: `exec`, `eval`, `compile`, `globals`, `vars`, `open` (unless filesystem provided), `dir`, `help`, `breakpoint`, `exit`, `quit`, `input`, `memoryview`.

**Policy-gated**: `__import__` -- see [Dynamic imports](#dynamic-imports) below.

`getattr()` and `hasattr()` are routed through the attribute policy -- they respect the same allow/deny rules as `obj.attr` syntax.

**Not available as names**: `BaseException`, `KeyboardInterrupt`, `GeneratorExit`, `SystemExit`.

### Dynamic imports

`__import__(name)` is available to sandboxed code and lands on the same policy
check as an `import` statement, so a computed module name is neither more nor
less permitted than a literal one:

```python
mod = __import__("math")        # fine if math is granted
mod = __import__(user_choice)   # ImportError unless the name is granted
```

This is safe because CPython keeps the two lookups apart. The `import`
statement resolves `__import__` from the frame's **builtins**, which is where
the sandbox parks the *real* `__import__` so C extensions (numpy, pandas) can
import their transitive dependencies. A source-level `__import__` is an
ordinary **name** load, and the rewriter redirects it to the `__st_dynimport__`
gate (`Rewriter.visit_Name`). Gating the name therefore never touches library
internals.

The redirect cannot be shadowed: `__import__` stays in `_BLOCKED_NAMES`, so
sandboxed code can't assign to it, delete it, or declare it `global`/`nonlocal`
to make the name fall through to the real builtin. `__builtins__` itself stays
unreadable, so the real `__import__` has no other route out.

Semantics match CPython, with one exception:

| Form | Result |
| --- | --- |
| `__import__("a.b")` | top-level package `a` |
| `__import__("a.b", fromlist=["c"])` | the leaf module `a.b` |
| `__import__(name, level=N)` for `N > 0` | `ImportError` -- use a `from . import ...` statement |
| `globals` / `locals` arguments | accepted and ignored (CPython uses them only for `level > 0`) |

A fromlist entry that is a lazily-loaded submodule is imported and bound, as
CPython does, so `__import__("PIL", fromlist=["Image"]).Image` resolves. An
*absent* entry is tolerated — `__import__(m, fromlist=["dummy"])` is the
standard idiom for "give me the leaf, not the top package". A submodule that
exists but *fails to load* is not tolerated: its error propagates, exactly as
`from m import sub` would report it. Collapsing that into a silent success is
how the two forms drift apart.

Relative dynamic imports are declined because the gate has no well-defined
caller package to resolve against; the statement form handles them.

**What you give up**: with literal imports, every module a script reaches for
is visible at rewrite time, so you can enumerate them by reading the code.
`__import__(computed)` moves that to runtime. The *policy* check is runtime
either way -- the security boundary is unchanged -- but static auditability of
the import set is not.

## What's blocked

- **Arbitrary imports** -- only policy-registered modules and VFS files, whether via an `import` statement or `__import__`
- **Private attributes** -- `_name` and `__dunder__` (except allowed dunders) blocked by default
- **Network I/O** -- socket operations blocked unless `allow_network=True`
- **File I/O** -- routes through VFS when filesystem provided, otherwise `open` unavailable
- **`type(name, bases, dict)`** -- three-arg form blocked (prevents dynamic class creation outside the rewriter)
- **`str.format` traversal** -- `"{0.__class__}".format(obj)` blocked
- **`__builtins__` access** -- blocked at the AST level; sandboxed code cannot read `__builtins__`
- **`__st_*` names** -- reserved namespace rejected at validation time
- **`globals()`** -- not available
- **Bare `except:`** -- automatically rewritten to `except Exception:`. Without this, sandboxed code could swallow `BaseException` subclasses that the sandbox relies on for control flow (`StTimeout`, `StCancelled`, `KeyboardInterrupt`, `SystemExit`), defeating timeouts and cancellation. This is a deliberate semantic change -- Python's bare `except:` normally catches everything, but in the sandbox it only catches `Exception` and below. No warning is emitted; the rewrite is silent and unconditional

## Checkpoint enforcement

Checkpoints are injected at:
- Start of every loop body (`for`, `while`)
- Start of every function/method body
- Every comprehension iteration (`[x for x in ...]`)
- Every call to a non-type builtin function (`len`, `sorted`, `sum`, etc.)

Type builtins (`str`, `int`, `dict`, `range`, etc.) do not fire checkpoints -- they are real types so that library code receiving them (e.g. `df.astype(str)`) works correctly. This means a single type construction like `list(range(10**8))` won't checkpoint before allocating. In practice this is not a gap: the allocation is a single C-level call that no per-call checkpoint could interrupt mid-flight, and the memory limit is enforced at the next checkpoint.

Each checkpoint increments the tick counter and checks: cancellation flag, tick limit, wall-clock timeout, and memory limit (in that order).

## Memory limits

When `Policy.memory_limit` is set (in MB), two layers of enforcement apply:

1. **RLIMIT_AS (Linux only)** -- kernel-enforced virtual address space cap. Set via `setrlimit` for the duration of the sandbox execution. The kernel refuses allocations beyond `current_RSS + limit`, raising `MemoryError`. This catches single large allocations that happen between checkpoints. This is **process-wide** -- concurrent sandboxes share the limit.

2. **Checkpoint-based detection** -- at each checkpoint, peak RSS (`ru_maxrss`) is compared against the baseline. If it exceeds the limit, `MemoryError` is raised. This works on both Linux and macOS but only fires at checkpoint boundaries.

macOS does not support `RLIMIT_AS` (`setrlimit` returns `EINVAL`), so only checkpoint-based detection is available. Windows lacks the `resource` module entirely -- memory limits are a no-op.

## Threat model

sandtrap is a "walled garden" -- it controls what sandboxed code can access, not what the Python runtime can do. It is designed to prevent accidental or casual misuse by LLM-generated code.

### In-process (`isolation="none"`)

The default mode runs in-process and shares the host's memory space. It is **not** a security boundary against a determined attacker with full CPython knowledge. For hard isolation, use `isolation="kernel"`.

### Subprocess (`isolation="process"` / `isolation="kernel"`)

`isolation="process"` runs sandboxed code in a forked child process. Its value is a **process boundary**: crashes, OOM, and segfaults in the child don't take down the host, namespaces are serialized across the boundary (so in-place mutations don't propagate back), and the child can be killed on timeout. It applies no kernel restrictions.

`isolation="kernel"` is `"process"` **plus** a best-effort kernel layer:

- **Filesystem** -- Landlock (Linux) or Seatbelt (macOS) restricts access to the `IsolatedFS` root directory
- **Syscalls** -- seccomp (Linux) or Seatbelt (macOS) blocks process spawning, and blocks network when the policy doesn't need it

#### What kernel mode is, and is not

Kernel mode is **defense-in-depth against accidental or casual escape** -- a buggy (not malicious) agent that makes an unintended network call, or a tool that tries to walk outside its root, is stopped at the kernel. That is its job, and it does it well when the mechanisms are available.

The sharpest form of that job is C extensions. A C library reaches the OS through syscalls rather than through the Python functions sandtrap patches, so an `open()` inside sqlite3 or a C parser never meets the VFS interception and a `fork`/`exec` inside a granted library never meets the builtins whitelist. Landlock (Linux) or Seatbelt (macOS) is the only thing keeping that C-level `open()` inside the `IsolatedFS` root, and seccomp (or Seatbelt) is the only thing stopping the process spawn. Under a cooperative threat model that is the whole of what kernel mode buys -- and it is worth buying.

Kernel mode is **not, today, a boundary against code that is actively trying to escape.** Two things stand between it and that guarantee:

1. **The inner layer is the Python AST sandbox, which is explicitly not adversarial-safe** (see In-process, above). Kernel restrictions only engage *after* code has broken out of the Python layer, and the whole point of the "walled garden" framing is that the Python layer is for cooperative code.
2. **The worker→host IPC channel uses `pickle`.** The parent process unpickles the worker's results and RPC arguments. seccomp permits the IPC syscalls (a worker must be able to return results), so code that escapes the Python layer inside the worker can write a crafted pickle to that channel and achieve code execution **in the host process on unpickle** -- bypassing seccomp/Landlock/Seatbelt entirely. `filter_namespace` runs worker-side and is self-sanitizing; it does not protect the host. Hardening this channel (a restricted unpickler / typed return contract for kernel mode) is on the [roadmap](roadmap.md); until it lands, do not rely on kernel mode to contain code you assume is hostile.

In short: use kernel mode to reduce the blast radius of mistakes and to add a real second layer under cooperative code. Do not use it as the sole containment for genuinely adversarial input.

#### Fail-closed by default

Kernel restrictions are applied once at worker startup and **cannot be loosened afterward** (seccomp/Landlock are strictly monotonic; Seatbelt is one-shot). If the platform can't apply what was requested -- missing package (`pip install sandtrap[kernel]`), kernel too old (Landlock needs 5.13+, often off in containers), or an unsupported OS -- `isolation="kernel"` **raises `IsolationUnavailable` at worker startup** rather than silently running user code with no kernel restrictions. Pass `allow_degraded=True` to proceed anyway; that emits a `RuntimeWarning` and records the shortfall.

Every `ExecResult` from a process/kernel sandbox carries an `isolation: IsolationStatus` describing exactly what took effect (`requested`, `platform`, and per-mechanism `landlock`/`seccomp`/`seatbelt` flags, plus a `.degraded` property). Inspect it to *verify* -- not assume -- that the restrictions you asked for are in force:

```python
with sandbox(policy, isolation="kernel", filesystem=IsolatedFS("/tmp/box")) as sb:
    result = sb.exec("...")
    assert not result.isolation.degraded   # or handle the shortfall
```

**Note on `network_access` / `host_fs_access`:** if any registration grants either, the corresponding kernel restriction is **completely off for the entire worker process** -- the kernel can't scope it to one callable, so only the Python-level `ContextVar` gating enforces it. This is a deliberate grant, not degradation, so it does not trigger the fail-closed path. See [process.md](process.md) for details.

### What's defended

- Attribute traversal attacks (MRO walking, `__subclasses__()`, `__globals__`)
- Dynamic class creation via `type(name, bases, dict)`
- Import of unregistered modules
- `str.format` field traversal (`{0.__class__}`)
- Swallowing control exceptions via bare `except:`
- Infinite loops and runaway computation (tick limits, timeouts)
- Unauthorized file and network I/O
- Process spawning and filesystem escape (`isolation="kernel"`, kernel-enforced)

### What's out of scope

These vectors are **not** defended against by the Python-level policy. `isolation="kernel"` mitigates some of them via kernel enforcement, but they remain concerns for in-process execution:

- **C extensions** -- a registered module with C code can do anything (call `ctypes`, access raw memory, spawn processes). Only register modules you trust. With `isolation="kernel"`, seccomp blocks `execve` and Landlock/Seatbelt restricts filesystem access, limiting the blast radius.
- **`ctypes` / `cffi`** -- if registered, these provide unrestricted access to the C layer. Never register them.
- **`gc.get_objects()`** -- if the `gc` module is registered, sandboxed code can enumerate all live Python objects. Don't register `gc`.
- **Signal handlers** -- `signal` module access would allow overriding the host's signal handling. With process isolation, this only affects the child process.
- **Shared mutable state** -- objects passed into the sandbox namespace are not copied. Sandboxed code can mutate them in place. With process isolation (`isolation="process"` or `"kernel"`), namespaces are serialized across the process boundary, so mutations don't propagate back to the host.
- **Worker→host deserialization** -- under `isolation="process"`/`"kernel"`, the host unpickles the worker's results and RPC arguments. Code that has already escaped the Python layer inside the worker can forge a malicious pickle and execute in the host on unpickle, bypassing kernel restrictions. This is why kernel mode is defense-in-depth, not an adversarial boundary (see the threat model above). Hardening it is on the [roadmap](roadmap.md).
- **Side channels** -- timing attacks, cache probing, and other side channels are not mitigated.
- **CPython internals** -- bytecode manipulation, `sys._getframe()`, `ctypes.pythonapi`, and other CPython-specific escape hatches are blocked by the attribute gate and builtins whitelist, but novel CPython exploits may bypass AST-level controls.

## Process-global patches

Filesystem interception is provided by [monkeyfs](https://github.com/ashenfad/monkeyfs), which monkey-patches `builtins.open`, `os.stat`, `os.path.exists`, and 20+ other stdlib functions at the process level. Network interception patches `socket.socket.connect` etc. similarly. Patches are installed once on first use and remain active permanently. They dispatch via `ContextVar` -- when no sandbox is executing, all calls fall through to the original functions transparently. This is necessary so that registered libraries (e.g., `pd.read_csv`) see the virtual filesystem during sandbox execution.
