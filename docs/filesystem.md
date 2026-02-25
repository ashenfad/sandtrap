# Filesystem & Network

## Virtual filesystem

Provide a `FileSystem` implementation to intercept all file I/O during sandboxed execution:

```python
from sandtrap import VirtualFS, Policy, Sandbox

fs = VirtualFS({})
fs.write("/data.txt", b"hello world")

with Sandbox(Policy(), filesystem=fs) as sandbox:
    result = sandbox.exec("""
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

## Network denial

By default, all socket operations are blocked. Sandboxed code that attempts network I/O gets an `OSError`:

```python
policy = Policy(allow_network=False)  # default
```

To allow network access globally:

```python
policy = Policy(allow_network=True)
```

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
