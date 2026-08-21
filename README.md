# sandtrap ⛳

A local Python sandbox using AST rewriting and compiled bytecode execution. Whitelist-based policies control attribute access, imports, and resource usage. Designed as a walled garden for cooperative code (e.g. agent-generated scripts), not for adversarial inputs.

Three isolation levels via the `sandbox()` factory:

- **`"none"`** (default) -- in-process, lightweight, shares the host's memory space
- **`"process"`** -- subprocess-backed, crash protection, no kernel restrictions
- **`"kernel"`** -- subprocess + kernel-level isolation (seccomp, Landlock, Seatbelt)

## Install

```
pip install sandtrap
```

That covers `"none"` and `"process"` on every platform, and `"kernel"` on macOS
(Seatbelt ships with the OS). Kernel mode **on Linux** needs seccomp and
Landlock bindings:

```
pip install sandtrap[kernel]
```

## Quick start

### In-process (default)

```python
from sandtrap import Policy, sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)

with sandbox(policy) as sb:
    result = sb.exec("""
total = sum(range(10))
print(f"total = {total}")
""")

print(result.stdout)       # "total = 45\n"
print(result.namespace)    # {"total": 45}
print(result.error)        # None
print(result.ticks)        # 2 (fn calls: sum + print)
```

### In a worker process

```python
from sandtrap import Policy, IsolatedFS, sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)

with sandbox(policy, isolation="process", filesystem=IsolatedFS("/tmp/sandbox")) as sb:
    result = sb.exec("""
total = sum(range(10))
print(f"total = {total}")
""")

print(result.stdout)       # "total = 45\n"
print(result.namespace)    # {"total": 45}
```

Swap `isolation="kernel"` for the same thing plus kernel-level restrictions.

## Worker-backed isolation

`"process"` and `"kernel"` both run code in a separate worker. What that buys,
and what it asks of you:

- **A crash stays contained.** A segfault or OOM in a C extension costs the
  call, not your process.
- **The worker inherits nothing** — not your memory, not your threads. It is
  forked from a dedicated broker, so a multi-threaded host can't deadlock it
  ([how workers are created](docs/process.md#how-workers-are-created)).
- **`"kernel"` adds a real boundary**: filesystem restricted to the
  `IsolatedFS` root via Landlock (Linux) or Seatbelt (macOS), syscall filtering
  via seccomp or Seatbelt, and network blocked at the kernel level unless the
  policy enables it.
- **Your policy must be serializable**, since it is sent rather than inherited.
  Module grants, module-level functions, and classes cross by name and need
  nothing; lambdas, closures, bound methods, and classes defined inside a
  function don't. `sandbox()` checks at construction and raises
  `StPolicyNotPortable` listing every problem at once, rather than surfacing
  pickle's first failure later from inside a worker — see
  [checking a policy is portable](docs/policy.md#checking-a-policy-is-portable),
  or pass `start_method="fork"` to keep inheriting memory and accept the
  deadlock hazard above.
- **Your entry point must be importable.** A worker that inherits no memory
  re-imports `__main__`, so module-level work there needs an
  `if __name__ == "__main__":` guard — and a host run as `python -c`, from a
  REPL, or from a piped heredoc has no importable `__main__` at all. Run the
  example above from a **file**. Servers are unaffected: an ASGI app is
  imported, not executed as `__main__`.
- **Workers cost more than a fork.** Each re-imports your granted modules
  rather than inheriting them: ~18ms for a stdlib policy, but ~235ms and
  ~113MB once a heavyweight stack is granted. `preload_grants=True` trades
  that back to ~14ms and ~29MB where your grants are safe to import in the
  broker ([the numbers](docs/process.md#what-the-default-costs-and-what-it-requires)).

Kernel mode is **defense-in-depth** — a second layer that contains accidental or casual escape (a buggy agent's stray network call, a walk outside the root) under the cooperative-code Python sandbox. It is **not** a boundary against code actively trying to escape: the inner Python layer isn't adversarial-safe, and the worker→host IPC uses `pickle`. See the [security model](docs/security.md#threat-model) for the full picture and the [roadmap](docs/roadmap.md) for hardening plans.

If the platform can't apply the requested kernel restrictions (missing `sandtrap[kernel]` packages, Landlock-less kernel, unsupported OS), `isolation="kernel"` **fails closed** — it raises `IsolationUnavailable` rather than silently running with no protection. Pass `allow_degraded=True` to proceed anyway; inspect `result.isolation` to see exactly what took effect.

## Part of the agex stack

sandtrap powers sandboxed code execution in [agex](https://github.com/ashenfad/agex), where AI agents write and execute Python directly against host libraries. Filesystem interception is provided by [monkeyfs](https://github.com/ashenfad/monkeyfs).

## Documentation

- [Policy & Registration](docs/policy.md) -- configuring what sandboxed code can access
- [Sandbox Execution](docs/sandbox.md) -- running code, results, error handling, REPL-style expression echo
- [Process Sandbox](docs/process.md) -- subprocess isolation with kernel-level restrictions
- [Filesystem & Network](docs/filesystem.md) -- VFS interception, network denial, VFS imports
- [Serialization](docs/serialization.md) -- pickling functions, classes, and state across turns
- [Security Model](docs/security.md) -- how the sandbox works, what it blocks, threat model
- [Roadmap](docs/roadmap.md) -- planned isolation hardening (restricted deserialization, kernel-mode boundary)
- [Forkserver design notes](docs/forkserver-design.md) -- why workers stopped being forked from the host, and what it cost

## License

MIT
