# sblite

A lightweight in-process Python sandbox using AST rewriting and compiled bytecode execution. Sandboxed code runs at near-native speed while attribute access, imports, and resource usage are controlled by a whitelist-based policy.

## Install

```
pip install sblite
```

## Quick start

```python
from sblite import Policy, Sandbox

policy = Policy(timeout=5.0, tick_limit=100_000)

with Sandbox(policy) as sandbox:
    result = sandbox.exec("""
total = sum(range(10))
print(f"total = {total}")
""")

print(result.stdout)       # "total = 45\n"
print(result.namespace)    # {"total": 45}
print(result.error)        # None
print(result.ticks)        # 0 (no loops or function calls)
```

## Documentation

- [Policy & Registration](docs/policy.md) -- configuring what sandboxed code can access
- [Sandbox Execution](docs/sandbox.md) -- running code, results, error handling
- [Filesystem & Network](docs/filesystem.md) -- VFS interception, network denial, VFS imports
- [Task Mode & Pickling](docs/task-mode.md) -- serializable functions and classes
- [Security Model](docs/security.md) -- how the sandbox works, what it blocks, threat model

## License

MIT
