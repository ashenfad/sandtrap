# Process Sandbox

`sandbox(policy, isolation="process")` and `sandbox(policy, isolation="kernel")` run sandboxed code in a forked child process. They provide the same `exec()`/`aexec()`/`cancel()` API as `isolation="none"`, but the child process is isolated from the host.

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

Same as `isolation="none"` -- all isolation levels return an `ExecResult` with `namespace`, `stdout`, `error`, and `ticks` fields.

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

### VirtualFS (in-memory)

Pass a `VirtualFS` or any other `FileSystem` implementation:

```python
from sandtrap import Policy, VirtualFS, sandbox

fs = VirtualFS({})
fs.write("/data.txt", b"hello from vfs")

with sandbox(Policy(timeout=10.0), isolation="process", filesystem=fs) as sb:
    result = sb.exec("content = open('/data.txt').read()")
    print(result.namespace["content"])  # "hello from vfs"
```

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
pip install sandtrap[process]
```

### macOS

**Seatbelt** -- applies an SBPL profile via `sandbox_init_with_parameters` (ctypes). Restricts filesystem to the `IsolatedFS` root plus system read-only paths (`/usr`, `/System/Library`, `/Library`). Blocks network when the policy doesn't need it. Graceful no-op if the API is unavailable.

No extra packages needed on macOS.

### Graceful degradation

All kernel isolation is best-effort. If the kernel features or packages aren't available, the sandbox still works -- the Python-level policy enforcement in `Sandbox` remains active. Warnings are emitted when kernel restrictions can't be applied.

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

- The worker process is forked eagerly when entering the context manager (`__enter__`)
- The worker persists across multiple `exec()` calls
- If the worker crashes (OOM, SIGKILL, seccomp violation), the next `exec()` automatically spawns a new one
- `shutdown()` sends a clean shutdown message; `__exit__` calls `shutdown()` automatically

## Namespace serialization

Namespaces are sent to and from the worker via `multiprocessing.Pipe` (pickle). Non-picklable values (lambdas, locks, etc.) are silently dropped from both input and output namespaces. A `RuntimeWarning` is emitted for each dropped input key.

`StFunction`, `StClass`, and `StInstance` wrappers are picklable and survive the process boundary. `exec()` returns active wrappers regardless of isolation level -- the same contract as in-process execution.
