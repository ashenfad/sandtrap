# Filesystem & Network

## Virtual filesystem

Provide a `FileSystem` implementation to intercept all file I/O during sandboxed execution:

```python
from sblite import MemoryFS, Policy, Sandbox

fs = MemoryFS()
fs.files["/data.txt"] = "hello world"

with Sandbox(Policy(), filesystem=fs) as sandbox:
    result = sandbox.exec("""
f = open('/data.txt')
content = f.read()
f.close()
""")
    assert result.namespace["content"] == "hello world"
```

When a filesystem is provided, all calls to `open()`, `os.stat()`, `os.listdir()`, `os.path.exists()`, `os.mkdir()`, `os.remove()`, `os.rename()`, `os.getcwd()`, `os.chdir()`, etc. route through the VFS.

### FileSystem protocol

`FileSystem` is a `typing.Protocol` -- any object with the right methods works, no subclassing required:

```python
class MyFS:
    def open(self, path, mode="r", **kwargs): ...
    def stat(self, path): ...
    def listdir(self, path): ...
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

You can also inherit from `FileSystem` for IDE autocompletion, but it's optional.

### MemoryFS

`MemoryFS` is a built-in in-memory implementation. Files are stored in `fs.files` (a `dict[str, str | bytes]`):

```python
fs = MemoryFS()
fs.files["/script.py"] = "x = 42"
fs.files["/data/config.json"] = '{"key": "value"}'
```

## VFS imports

Sandboxed code can `import` modules from Python files in the VFS:

```python
fs.files["/helpers.py"] = "def double(x): return x * 2"

result = sandbox.exec("""
from helpers import double
result = double(5)
""")
assert result.namespace["result"] == 10
```

### Relative imports

VFS modules support relative imports:

```python
fs.files["/pkg/utils.py"] = "def add(a, b): return a + b"
fs.files["/pkg/main.py"] = "from .utils import add"
```

### Package directories

Directories work as packages without `__init__.py`:

```python
fs.files["/mylib/core.py"] = "value = 42"

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
