# Process Sandbox

`sandbox(policy, isolation="process")` and `sandbox(policy, isolation="kernel")` run sandboxed code in a separate worker process — by default one that does *not* inherit your process's memory (see [How workers are created](#how-workers-are-created)). They provide the same `exec()`/`aexec()`/`cancel()` API as `isolation="none"`, but the child process is isolated from the host.

- **`isolation="process"`** -- subprocess-backed execution with crash protection. No kernel-level restrictions.
- **`isolation="kernel"`** -- subprocess + kernel-level filesystem restriction, syscall filtering, and network blocking.

## When to use it

Use `isolation="kernel"` when:

- A crash, memory blowup, or segfault in sandboxed code must not affect the host process
- You want kernel-enforced filesystem and network restrictions as a hard backstop
- You're running untrusted code in a server environment

Use `isolation="process"` when:

- You need crash protection but don't need kernel restrictions
- You're developing/debugging and want to skip kernel lockdown

Use `isolation="none"` (default) when:

- You want the lowest overhead (no fork, no IPC)
- You don't need process isolation (e.g., local notebooks, trusted agent code)

## Creating a process sandbox

```python
from sandtrap import Policy, IsolatedFS, sandbox

policy = Policy(timeout=10.0, tick_limit=100_000)

with sandbox(policy, isolation="kernel", filesystem=IsolatedFS("/tmp/sandbox")) as sb:
    result = sb.exec("x = 2 + 3")
    print(result.namespace["x"])  # 5
```

All `sandbox()` parameters are documented in [sandbox.md](sandbox.md). The process-relevant ones:

- `isolation` -- `"process"` or `"kernel"`.
- `filesystem` -- a `monkeyfs.FileSystem` implementation (e.g., `IsolatedFS`, `VirtualFS`). Optional -- when `None`, sandboxed code has no file I/O. When an `IsolatedFS` is provided with `isolation="kernel"`, kernel-level filesystem restriction locks access to its root directory.
- `snapshot_prints` -- works across all isolation levels. When `True`, `result.prints` contains deep-copied `print()` arguments from the worker, pickled back with the result.
- `start_method` -- how the worker is created; `None` (default) picks the safest available. `"fork"` is the escape hatch for a policy that can't be serialized. See [How workers are created](#how-workers-are-created).
- `preload_grants` -- import your granted modules into the forkserver broker so workers inherit them. Off by default; a large win where it's safe. See [What the default costs](#what-the-default-costs-and-what-it-requires).
- `allow_degraded` -- `isolation="kernel"` only. Proceed with a warning when the platform can't apply the requested kernel mechanisms, instead of raising. See [Fail-closed when isolation is unavailable](#fail-closed-when-isolation-is-unavailable).
- `rpc_handlers` -- bridge live parent-side objects into the worker. See [Exposing a live host object](policy.md#exposing-a-live-host-object).
- `close_fds` -- neutralize ambient host descriptors in the worker. See [Host file descriptors](#host-file-descriptors).

The last five are ignored under `isolation="none"`, so one config can drive every rung.

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
result = await sb.aexec("x = 42")
```

## ExecResult

Same as `isolation="none"` -- all isolation levels return an `ExecResult` with `namespace`, `stdout`, `stderr`, `error`, and `ticks` fields. `stdout`/`stderr` are captured inside the worker (where registered library code actually runs) and shipped back with the result -- including host-library writes to the real streams, like `df.info()`.

## Cancellation

Cancel from another thread:

```python
import threading

timer = threading.Timer(1.0, sb.cancel)
timer.start()
result = sb.exec("while True: pass")
```

`cancel()` sends `SIGUSR1` to the worker process, which triggers cancellation in the child.

## Filesystem options

### IsolatedFS (real directory)

Pass an `IsolatedFS` to map all paths to a real directory on disk:

```python
import os
from sandtrap import Policy, IsolatedFS, sandbox

with sandbox(Policy(timeout=10.0), isolation="kernel", filesystem=IsolatedFS("/tmp/sandbox")) as sb:
    # Host writes a file into the sandbox root
    with open(os.path.join("/tmp/sandbox", "data.txt"), "w") as f:
        f.write("hello")

    # Sandboxed code reads it at /data.txt
    result = sb.exec("content = open('/data.txt').read()")
    assert result.namespace["content"] == "hello"
```

Files written by sandboxed code appear in the root directory on the host. With `isolation="kernel"`, kernel-level filesystem restriction locks access to the `IsolatedFS` root.

### VirtualFS (in-memory) — bridged over RPC

Pass a `VirtualFS` or any other `FileSystem` implementation:

```python
from sandtrap import Policy, VirtualFS, sandbox

fs = VirtualFS({})
fs.write("/data.txt", b"hello from vfs")

with sandbox(Policy(timeout=10.0), isolation="process", filesystem=fs) as sb:
    result = sb.exec("open('/reply.txt', 'w').write('hello from the worker')")
    print(fs.read("/reply.txt"))  # b"hello from the worker" — the PARENT's fs
```

Non-`IsolatedFS` filesystems are **bridged over the RPC channel**: the parent keeps the real instance and registers an internal handler; the worker sees a `RemoteFS` stub whose every operation is a synchronous RPC. The parent's filesystem stays the single source of truth — worker writes land in it, `chdir` moves its cwd, and a worker crash loses nothing already written. (Fork-inheriting an in-memory fs would hand the worker a divergent copy whose writes silently vanish.)

File handles are whole-blob buffered, matching monkeyfs semantics: read modes fetch content once at `open`; writable modes buffer locally and push on `flush`/`close`. Seeks, iteration, and partial reads are local and cost no round-trips.

When using a non-`IsolatedFS` filesystem, no kernel-level filesystem restriction is applied (there's no host path to restrict). Seccomp and network isolation still apply.

### No filesystem

When `filesystem=None` (default), sandboxed code has no file I/O. This works with all isolation levels.

## Kernel isolation

When `isolation="kernel"`, platform-appropriate kernel restrictions are applied in the child process before any user code runs.

### Linux

1. **Landlock** (kernel 5.13+) -- restricts filesystem access to the `IsolatedFS` root directory (when provided). Requires the `landlock` PyPI package. Graceful no-op if the kernel doesn't support it.

2. **seccomp** -- installs a syscall allowlist. Blocks `execve`, process spawning, and (when the policy doesn't need network) `socket`/`connect`/`bind`/`listen`. Requires the `pyseccomp` PyPI package and `libseccomp`. Graceful no-op if unavailable.

Install both with:

```
pip install sandtrap[kernel]
```

### macOS

**Seatbelt** -- applies an SBPL profile via `sandbox_init_with_parameters` (ctypes). Restricts filesystem to the `IsolatedFS` root plus system read-only paths (`/usr`, `/System/Library`, `/Library`). Blocks network when the policy doesn't need it. Graceful no-op if the API is unavailable.

No extra packages needed on macOS.

### Fail-closed when isolation is unavailable

Kernel isolation is best-effort in the sense that the *mechanisms* may not exist on a given platform (missing `sandtrap[kernel]` packages, Landlock-less kernel, unsupported OS). But `isolation="kernel"` does **not** silently downgrade when they're missing — that would run user code with none of the protection the caller asked for.

Instead, the worker records exactly what took effect and the parent **fails closed**: if kernel isolation was requested but couldn't be fully applied, entering the sandbox raises `IsolationUnavailable` before any user code runs.

```python
from sandtrap import IsolationUnavailable, Policy, sandbox

try:
    with sandbox(Policy(), isolation="kernel", filesystem=IsolatedFS("/tmp/box")) as sb:
        sb.exec("...")
except IsolationUnavailable as e:
    # e.g. running in a container without Landlock, or sandtrap[kernel] not installed
    ...
```

To proceed anyway with whatever isolation *is* available (e.g. seccomp without Landlock), pass `allow_degraded=True`. This downgrades the failure to a `RuntimeWarning` and records the shortfall:

```python
with sandbox(Policy(), isolation="kernel", allow_degraded=True) as sb:
    result = sb.exec("...")
    print(result.isolation)            # IsolationStatus(...)
    print(result.isolation.degraded)   # True if something couldn't be applied
```

Either way, the Python-level policy enforcement in `Sandbox` is always active regardless of kernel availability. The fail-closed default only governs whether a *missing kernel layer* is treated as an error.

#### Inspecting what was applied

Every `ExecResult` from a process/kernel sandbox carries an `isolation: IsolationStatus`:

| Field | Meaning |
|-------|---------|
| `requested` | `True` for `isolation="kernel"`, `False` for `"process"` |
| `platform` | `sys.platform` in the worker |
| `landlock` / `seccomp` / `seatbelt` | `True` applied, `False` requested-but-unavailable, `None` not applicable |
| `.degraded` | `True` if requested isolation wasn't fully applied |

This lets a host *verify* the isolation level rather than assume the requested one was achievable — useful when the same code runs across dev laptops, CI, and containers with different kernel support.

## Kernel enforcement is conditional

Kernel-level restrictions are applied once when the worker process starts and **cannot be loosened afterward** -- seccomp and Landlock are strictly monotonic (can only get more restrictive), and Seatbelt is completely one-shot (cannot be modified at all after application). This means the kernel profile must be permissive enough for anything the policy might need during the worker's lifetime.

**Important:** If even a single registration in the policy has `network_access=True` or `host_fs_access=True`, the corresponding kernel-level restriction is **completely disabled for the entire worker process for its entire lifetime.** The kernel cannot selectively allow network or filesystem access only during a specific callable's execution. In that case, only the Python-level `ContextVar`-based gating controls per-callable access.

### Network

If `policy.allow_network` is `True`, or if any registered function, class, or module has `network_access=True`, the kernel allows **all** network syscalls for the worker process. Only the Python-level gating restricts which callables can actually use the network.

If no part of the policy needs network, the kernel blocks `socket`/`connect`/`bind`/`listen` as a hard backstop that no Python-level bypass can circumvent.

### Filesystem

Kernel filesystem lockdown (Landlock on Linux, Seatbelt on macOS) is applied only when **both** conditions hold:

1. An `IsolatedFS` is provided as the `filesystem` (so there's a host path to lock down)
2. No part of the policy has `host_fs_access=True`

If any registered function, class, or module has `host_fs_access=True`, the kernel allows **full host filesystem access** for the worker process. Only the Python-level `suspend()` mechanism controls when VFS interception is bypassed.

If using `VirtualFS` or another non-`IsolatedFS` filesystem, there's no host path for the kernel to restrict, so filesystem lockdown is skipped.

## Worker lifecycle

- The worker process is started eagerly when entering the context manager (`__enter__`)
- The worker persists across multiple `exec()` calls
- If the worker crashes (OOM, SIGKILL, seccomp violation), the next `exec()` automatically spawns a new one
- An idle worker observes EOF and exits if the parent process disappears, even while a later worker remains busy. Under `start_method="fork"` this needs care, since each child inherits copies of every active Sandtrap control endpoint and closes them; a worker that inherited nothing has none to close
- `shutdown()` sends a clean shutdown message; `__exit__` calls `shutdown()` automatically

### Host file descriptors

Only relevant under `start_method="fork"`. A worker that doesn't inherit your
memory doesn't inherit your descriptors either, so there is nothing to close
and `close_fds` is a no-op there.

Fork copies the embedding process's open file descriptors even when they are
marked close-on-exec. Under fork, Sandtrap preserves that by default, because a
policy-registered function or object may intentionally depend on a live
inherited resource.

Pass `close_fds=True` to neutralize ambient descriptors in the child before
worker initialization, preserving only standard streams and the worker's
private multiprocessing plumbing. This applies to both process and kernel
isolation:

```python
with sandbox(policy, isolation="process", close_fds=True) as sb:
    ...
```

With descriptor cleanup enabled, policy registrations cannot rely on an
already-open host socket, database connection, pipe, or file handle surviving
into the worker. Keep the live resource in the parent and expose the required
operations through an [RPC handler](serialization.md#cross-process-resources-via-rpc-process--kernel-isolation).
This makes ownership explicit and prevents a worker from keeping unrelated
host resources alive. Leave `close_fds=False` only when inherited live state
is an intentional part of the policy contract.

## How workers are created

By default a worker is **not** forked from your process. A broker is started
once, from a fresh interpreter, and forks each worker from it — `forkserver`,
selected automatically. Override with `start_method=`, on either constructor:

```python
sandbox(policy, isolation="process", start_method="fork")   # the factory
ProcessSandbox(policy, start_method="fork")                 # or directly
```

`preload_grants=` rides along the same way. Both were reachable only through
`ProcessSandbox` before 0.3.2, which made the escape hatch below unusable from
`sandtrap.sandbox()` — the only constructor in `sandtrap.__all__`.

(Process and kernel isolation are POSIX-only: cancellation is delivered with
`SIGUSR1`, which Windows has no equivalent of. The start method is chosen from
what the platform offers, so `spawn` would be selected where forkserver is
absent, but that path is untested.)

The reason is below, and it's the whole point: because the broker never grows
threads, no worker can inherit a lock your process holds.

### Why not fork

`fork()` duplicates only the calling thread, so a lock another thread holds at
that instant is inherited already-held by a child with no thread left to
release it. Such a child does not crash — it **hangs**, on first contention,
until your own timeout fires and reports something unrelated-sounding
(`"Worker process became unresponsive"`).

A host that has accumulated threads — a web server, browser automation, SSE
streams — has no quiet moment to fork from. That is not an unusual
configuration: uvicorn is multi-threaded by construction, and CPython agrees
the pattern is a hazard (3.12 warns on fork from a multi-threaded process; 3.14
moves multiprocessing's Linux default off fork).

Fork-hostile C-library state produces a related, louder failure: children that
die instantly, in a **`"Worker process died during initialisation"` loop**,
because the automatic respawn re-forks the *current* (still hostile) process on
every attempt.

sandtrap raises `StForkUnsafe` on that signature rather than looping
quietly. The error names the cause it can see -- the worker's exit signal
and the host's live thread count -- and lists the fixes below. It is
raised only when the worker died from a *native crash signal*
(`SIGSEGV`, `SIGBUS`, `SIGABRT`, …), which is what fork-broken C-library
state produces. A worker that exits nonzero, is `SIGKILL`ed by an OOM
killer, or fails in Python-level setup reports its own cause instead. It is a
subclass of both `StError` and `RuntimeError`, so existing `except
RuntimeError` handlers keep working.

**None of this is reachable on the default start method** — a worker that
doesn't inherit your memory cannot inherit broken allocator state from it.
`StForkUnsafe` is raised only under `start_method="fork"`. The rules below
apply if you opt into it.

- **Construct sandboxes early**, before your host grows threads. The eager
  worker start at `__enter__` exists for exactly this reason.
- **Known offender: pyarrow's default mimalloc pool.** Its per-thread
  heaps don't survive fork; a forked child segfaults inside `libarrow`
  (`mi_thread_init`) on its first arrow allocation — macOS crash
  reports annotate it `*** multi-threaded process forked ***`. And
  `import pandas` (3.x) imports pyarrow. Fix in the EMBEDDER, before
  the first pandas/pyarrow import anywhere in the process:

  ```python
  os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")
  ```

  pyarrow reads the variable at import time; setting it later is a
  no-op. Not needed on the default start method: the broker never imports
  your grants (see `preload_grants`), so no arrow state exists to inherit.
- macOS is the strictest platform (the Objective-C runtime aborts
  forked children that touch certain frameworks), but allocator
  thread-state hazards exist on Linux too.
- CPython agrees fork-with-threads is a hazard: 3.12 deprecates it and
  3.14 moves the multiprocessing default away from fork on Linux.
- **`preload_grants=True` reopens a narrow version of this.** It imports
  your granted modules into the broker, so a grant that leaves allocator
  or thread state there is inherited by every worker forked from it. The
  arrow fix above applies to that configuration too.

### What the default costs, and what it requires

The broker preloads sandtrap itself, so workers inherit it rather than
importing it:

| start method | worker start + one exec |
|---|---|
| `fork` (opt-in; inherits your memory) | ~4.8 ms |
| `forkserver` + `preload_grants=True` | ~5.5 ms |
| **`forkserver` (the default)** | **~18 ms** |
| `forkserver`, nothing preloaded | ~42 ms |
| `spawn` | ~77 ms |

*(41-grant stdlib policy, macOS/CPython 3.12. Median of repeated starts.)*

Those are stdlib numbers. **Grant a heavyweight stack and the gap becomes the
dominant cost of a worker**, because every worker imports its own copy:

| pandas + numpy + plotly + matplotlib granted | worker start | worker RSS |
|---|---|---|
| **default (grants not preloaded)** | **~235 ms** | **~113 MB** |
| `preload_grants=True` | ~14 ms | ~29 MB |

*(Same host. RSS is `ps` resident size for the worker process.)*

The memory difference is not a rounding artifact: preloaded modules live in the
broker and workers share those pages copy-on-write, so the stack is paid for
once rather than per worker. Size a pool of workers with the right row — the
default's ~113 MB apiece is what a resident worker actually holds, before any
of your data.

**`preload_grants=True` also imports your granted modules into the broker.**
It is off by default because preloading runs their *import-time code there*: a
grant that starts a background thread on import leaves the broker
multi-threaded, and a worker forked from it can inherit a lock held by that
thread — the same permanent hang this default exists to prevent. Your grants
are yours, so only you can say whether that is true of them. Turn it on when
you know they start no threads on import.

What the default requires:

- **The policy must be serializable**, since it is sent rather than inherited.
  Checked at construction, so an unportable policy raises
  `StPolicyNotPortable` where you wrote it. What crosses, what doesn't, and how
  to check before you get there:
  [Checking a policy is portable](policy.md#checking-a-policy-is-portable).
- **Modules are re-imported, not inherited**, so the worker gets fresh
  C-library state (the point) but loses any host-side monkeypatching.
- **`__main__` must be import-safe.** The child re-imports it, so module-level
  work in your entry point needs an `if __name__ == "__main__":` guard. A host
  run as `python -c` or from a REPL has no importable `__main__` at all.
  Servers are unaffected — an ASGI app is imported, not run as `__main__`.
- **`close_fds` is a no-op**: nothing is inherited to close.
- **A worker's parent is the broker**, not your process, so `os.getppid()`
  inside a worker doesn't name the embedding process.

`start_method="fork"` remains available as the escape hatch for a policy that
can't be serialized — explicitly, never as a silent fallback, because falling
back quietly would put you back on the hanging path without saying so.

The preload list is process-global and read once, when the broker starts: the
first worker started in your process fixes it. A sandbox created later with
different grants still works — its modules are simply imported in the worker
rather than inherited.

This matters most for `preload_grants`, which is therefore **effectively a
process-wide setting wearing per-sandbox clothes**: only the first sandbox to
start a worker can turn it on. A host that builds many sandboxes (one per
session, say) should set it uniformly, or set it on the first. Asking for a
preload the running broker doesn't have emits a `RuntimeWarning` naming the
modules that won't be inherited — otherwise the flag looks accepted while
worker start stays slow, which is indistinguishable from it being broken.

**Pre-fork servers.** multiprocessing tracks the broker in a module-level
singleton and registers no after-fork hook, so a process forked from one that
already started a broker inherits a pid that isn't its child. gunicorn and
`uvicorn --workers N` fork their workers from a supervisor, so they hit this
whenever a broker was started before the fork. sandtrap detects the resulting
`ChildProcessError`, drops the inherited bookkeeping, and lets that process
start its own broker.

## Namespace serialization

Namespaces are sent to and from the worker via `multiprocessing.Pipe` (pickle). Non-picklable values (lambdas, locks, etc.) are silently dropped from both input and output namespaces. A `RuntimeWarning` is emitted for each dropped input key.

Under the default `mode="raw"`, sandbox-defined functions and classes are plain objects and pickle no better than any other locally-defined function, so they do not come back from the worker. Under the deprecated `mode="wrapped"`, `StFunction`, `StClass`, and `StInstance` wrappers are picklable and survive the process boundary; `exec()` returns them active regardless of isolation level -- the same contract as in-process execution.
