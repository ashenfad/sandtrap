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

For subprocess isolation with kernel-level sandboxing on Linux:

```
pip install sandtrap[process]
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

### Subprocess

```python
from sandtrap import Policy, IsolatedFS, sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)

with sandbox(policy, isolation="kernel", filesystem=IsolatedFS("/tmp/sandbox")) as sb:
    result = sb.exec("""
total = sum(range(10))
print(f"total = {total}")
""")

print(result.stdout)       # "total = 45\n"
print(result.namespace)    # {"total": 45}
```

`isolation="kernel"` runs code in a forked child process with:
- Filesystem restricted to the `IsolatedFS` root via Landlock (Linux) or Seatbelt (macOS)
- Syscall filtering via seccomp (Linux) or Seatbelt (macOS)
- Network blocked at the kernel level (unless the policy enables it)
- Worker crash doesn't take down the host process

Kernel mode is **defense-in-depth** — a second layer that contains accidental or casual escape (a buggy agent's stray network call, a walk outside the root) under the cooperative-code Python sandbox. It is **not** a boundary against code actively trying to escape: the inner Python layer isn't adversarial-safe, and the worker→host IPC uses `pickle`. See the [security model](docs/security.md#threat-model) for the full picture and the [roadmap](docs/roadmap.md) for hardening plans.

If the platform can't apply the requested kernel restrictions (missing `sandtrap[process]` packages, Landlock-less kernel, unsupported OS), `isolation="kernel"` **fails closed** — it raises `IsolationUnavailable` rather than silently running with no protection. Pass `allow_degraded=True` to proceed anyway; inspect `result.isolation` to see exactly what took effect.

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

## License

MIT
