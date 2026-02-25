# sandtrap ⛳

A lightweight in-process Python sandbox using AST rewriting and compiled bytecode execution. Whitelist-based policies control attribute access, imports, and resource usage. Designed as a walled garden for cooperative code (e.g. agent-generated scripts), not for adversarial inputs.

## Install

```
pip install sandtrap
```

## Quick start

```python
from sandtrap import Policy, Sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)

with Sandbox(policy) as sandbox:
    result = sandbox.exec("""
total = sum(range(10))
print(f"total = {total}")
""")

print(result.stdout)       # "total = 45\n"
print(result.namespace)    # {"total": 45}
print(result.error)        # None
print(result.ticks)        # 2 (fn calls: sum + print)
```

## Documentation

- [Policy & Registration](docs/policy.md) -- configuring what sandboxed code can access
- [Sandbox Execution](docs/sandbox.md) -- running code, results, error handling
- [Filesystem & Network](docs/filesystem.md) -- VFS interception, network denial, VFS imports
- [Serialization](docs/serialization.md) -- pickling functions, classes, and state across turns
- [Security Model](docs/security.md) -- how the sandbox works, what it blocks, threat model

## License

MIT
