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

### Writes lost at an implicit close

A file the code never closes (`open(p, "w").write(x)`) is saved when its last reference goes, as a real file is. If that save fails -- the filesystem refuses the write, a quota is full -- the code has already moved on, so the failure can't be raised where it wrote. The execution fails instead:

- If it had no error of its own, `result.error` is `StLostWrite`, an `StError` and an `OSError`, whose `filename` is the file's path, whose `errno` is the failure's (`EIO` when it has none), and whose `__cause__` is the exception the save raised. Any further lost write is a note on it.
- If it had already failed, its error stays, with a note naming each lost file (`add_note`). Python 3.10 has no `add_note`: the note goes on `__notes__` all the same, and is also written to `result.stderr`, since 3.10's tracebacks don't show it.

An explicit `close()` or `with` block still raises the failure into the code, and a save that succeeds changes nothing. This holds for `exec` and `aexec` at every isolation level, for files over a `VirtualFS` or any other non-`IsolatedFS` filesystem. In-process, it needs a monkeyfs that reports the loss (one with `monkeyfs.LostWriteError`); an older one discards it. Files over `IsolatedFS` are real files, finalized by Python's `io` module, and aren't covered.

A loss is charged to the execution whose context finalized the file, through `sys.unraisablehook`: sandtrap puts one hook in front of whatever hook is installed and passes on everything that isn't a lost write. So a file still open when the execution ends -- bound in `result.namespace`, or held by a failed execution's traceback -- fails nothing when it is collected later: that loss goes to the previous hook, which by default prints it to stderr. The same goes for a file in a reference cycle, which is saved whenever the cyclic garbage collector runs, possibly during another execution, which it then fails. Close files explicitly to keep the failure where it happened.

Under process isolation, a file still open when the sandbox closes is not saved at all: the worker finalizes it as it exits, after its parent has stopped answering, so the save is refused and nothing is reported.

A save that fails at an explicit `close()` or, in a worker, `flush()` has already been raised to the code; the file is not saved again when collected, and nothing more is reported, unless the code changes the file's content afterwards. That new content was never reported, so losing it fails the execution as above.

### Library data files

Granted libraries can still read their own bundled data while a VFS is
active (matplotlib's fonts, tzdata, package templates): monkeyfs allows
read-only passthrough for paths under the running interpreter's
`sys.prefix` / site-packages, discovered at import time.

That discovery assumes granted libraries are installed *into the
environment that runs the sandbox*. Two layouts break the assumption:

- **Layered/overlay environments** (`uv run --with ...`, `uvx`): the
  overlay becomes `sys.prefix`, so packages in the base environment
  fall outside the passthrough roots.
- **Editable installs** (`pip install -e`): the package lives in its
  source checkout, on `sys.path` but under no interpreter prefix.

In either case a library reading its own data files fails with a
`FileNotFoundError` for a file that plainly exists on disk. The fix is
to install granted libraries into the sandbox's own environment (a
regular, non-editable install).

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

A VFS module's execution namespace is its `__dict__`, so it behaves like any
imported Python module: setting an attribute on it changes what its own code
reads, and `unittest.mock.patch.object` on one of its functions is what the
module itself calls.

```python
fs.write("/h.py", b"LIMIT = 10\n\ndef get():\n    return LIMIT\n")

result = sandbox.exec("""
import h
h.LIMIT = 99
inside = h.get()
""")
assert result.namespace["inside"] == 99
```

A module body that raises leaves nothing importable behind: the module is
dropped, and the next import of that name runs the body again from scratch.

A VFS module exports what its body defined, and nothing else. The gates and
builtins it runs on live in the same `__dict__`, so `__builtins__` and the
`__st_*` gates are refused as bare names, as attributes, and as
`from <module> import` targets, and `dir()` does not list them. Nor does a
from-import reach the *module type* -- `from h import __getattribute__` and
`from h import __dict__` are refused, since either one answers for the whole
dict. No other dunder is importable from a workspace module, including one the
body assigned, except a module's metadata: `__version__`, `__author__` and
`__all__` read as attributes and import as names, out of the module's own
namespace and only as data (an exact `str`, or for `__all__` a list of them,
copied). So `from pkg import __version__` in `pkg/cli.py` works as it does
outside the sandbox.

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
