# Filesystem & Network

## Virtual filesystem

Provide a `FileSystem` implementation to intercept all file I/O during sandboxed execution:

```python
from sandtrap import VirtualFS, Policy, sandbox

fs = VirtualFS({})
fs.write("/data.txt", b"hello world")

with sandbox(Policy(), filesystem=fs) as sb:
    result = sb.exec("""
f = open('/data.txt')
content = f.read()
f.close()
""")
    assert result.namespace["content"] == "hello world"
```

When a filesystem is provided, all calls to `open()`, `os.stat()`, `os.listdir()`, `os.path.exists()`, `os.mkdir()`, `os.remove()`, `os.rename()`, `os.getcwd()`, `os.chdir()`, `os.path.isfile()`, `os.path.isdir()`, `os.path.realpath()`, `os.path.expanduser()`, `pathlib.Path.touch()`, and more route through the VFS. Interception is powered by [monkeyfs](https://github.com/ashenfad/monkeyfs), which sandtrap uses as a dependency.

### FileSystem protocol

`FileSystem` is a `typing.Protocol` from monkeyfs -- any object with the right methods works, no subclassing required. See the [monkeyfs README](https://github.com/ashenfad/monkeyfs) for the full protocol. The most commonly needed methods:

```python
class MyFS:
    def open(self, path, mode="r", **kwargs): ...
    def stat(self, path): ...
    def list(self, path="."): ...       # immediate children
    def listdir(self, path="."): ...
    def exists(self, path): ...
    def isfile(self, path): ...
    def isdir(self, path): ...
    def mkdir(self, path, *, parents=False, exist_ok=False): ...
    def makedirs(self, path, *, exist_ok=False): ...
    def remove(self, path): ...
    def rename(self, src, dst): ...
    def getcwd(self): ...
    def chdir(self, path): ...
```

## VFS imports

Sandboxed code can `import` modules from Python files in the VFS:

```python
fs.write("/helpers.py", b"def double(x): return x * 2")

result = sandbox.exec("""
from helpers import double
result = double(5)
""")
assert result.namespace["result"] == 10
```

### Relative imports

VFS modules support relative imports:

```python
fs.write("/pkg/utils.py", b"def add(a, b): return a + b")
fs.write("/pkg/main.py", b"from .utils import add")
```

### Package directories

Directories work as packages without `__init__.py`:

```python
fs.write("/mylib/core.py", b"value = 42")

result = sandbox.exec("from mylib import core")
# core.value == 42
```

## Real filesystem (IsolatedFS)

Use `IsolatedFS` to map all paths to a real directory on disk:

```python
from sandtrap import Policy, IsolatedFS, sandbox

with sandbox(Policy(), isolation="kernel", filesystem=IsolatedFS("/tmp/sandbox")) as sb:
    result = sb.exec("""
f = open('/output.txt', 'w')
f.write('hello')
f.close()
""")
```

The file appears at `/tmp/sandbox/output.txt` on the host. See [process.md](process.md) for details.

With `isolation="kernel"`, kernel-level enforcement (Landlock, Seatbelt) also restricts the child process to the `IsolatedFS` root at the OS level -- but only when no part of the policy has `host_fs_access=True`. If any registration needs host filesystem access, the kernel allows full filesystem access and the Python-level interception handles per-callable gating.

## VirtualFS with process isolation

`VirtualFS` and any other `FileSystem` implementation work with all isolation levels:

```python
from sandtrap import Policy, VirtualFS, sandbox

fs = VirtualFS({})
fs.write("/data.txt", b"hello")

with sandbox(Policy(timeout=10.0), isolation="process", filesystem=fs) as sb:
    result = sb.exec("content = open('/data.txt').read()")
```

When using a non-`IsolatedFS` filesystem, kernel-level filesystem restriction is not applied (there is no host path to restrict). See [process.md](process.md#filesystem-options) for more.

## Network denial

By default, all socket operations are blocked. Sandboxed code that attempts network I/O gets an `OSError`:

```python
policy = Policy(allow_network=False)  # default
```

To allow network access globally:

```python
policy = Policy(allow_network=True)
```

With `isolation="kernel"`, network blocking is also enforced at the kernel level (seccomp on Linux, Seatbelt on macOS). The kernel blocks `socket`, `connect`, `bind`, and `listen` syscalls when no part of the policy needs network access. If the policy enables network (globally or per-registration), the kernel allows network syscalls and the Python-level gating handles per-callable access control.

### Per-registration access

Grant network or filesystem access to specific functions, classes, or modules:

```python
@policy.fn(network_access=True)
def fetch(url):
    import requests
    return requests.get(url).text

policy.module(my_db, host_fs_access=True)
```

These run with temporary privilege grants -- the sandbox code itself remains restricted.

**`isolation="kernel"` caveat:** Kernel restrictions (seccomp, Landlock, Seatbelt) are applied once at worker startup and cannot be changed afterward. If any registration has `network_access=True` or `host_fs_access=True`, the corresponding kernel-level restriction is **completely disabled for the entire worker process for its entire lifetime.** The Python-level `ContextVar` gating is the only thing ensuring that only the privileged callable can actually use the network or host filesystem. See [process.md](process.md#kernel-enforcement-is-conditional) for details.
