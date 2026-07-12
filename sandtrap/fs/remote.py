"""RemoteFS: a filesystem whose operations ride the process sandbox's
RPC channel back to the parent.

Fork-inheriting an in-memory filesystem (``VirtualFS``) into the worker
hands the worker a divergent COPY — its writes never reach the parent.
``RemoteFS`` keeps the single source of truth in the parent process:
every operation is a synchronous RPC to a parent-side handler wrapping
the real filesystem (:func:`fs_rpc_handler`). ``ProcessSandbox`` wires
this automatically for any non-``IsolatedFS`` filesystem — embedders
don't construct these directly.

File handles are whole-blob buffered, matching monkeyfs semantics
(``VirtualFS`` materializes whole files anyway): read modes fetch the
content once at ``open``; writable modes buffer locally and push on
``flush``/``close``. Seeks, iteration, and partial reads are local.
"""

from __future__ import annotations

import io
from typing import Any


class RemoteFSMarker:
    """Sentinel passed to the worker (via fork) in place of the real
    filesystem: 'build a RemoteFS against the ``__fs__`` handler'."""


FS_RPC_TARGET = "__fs__"

# Methods the parent-side handler will dispatch onto the wrapped
# filesystem. An allowlist, not getattr-anything: the RPC channel
# reaches OUT of the sandbox, so the surface stays enumerable.
_FS_METHODS = frozenset(
    {
        "stat",
        "exists",
        "isfile",
        "isdir",
        "list",
        "remove",
        "mkdir",
        "makedirs",
        "rename",
        "getcwd",
        "chdir",
        # the rest of the monkeyfs surface its patch layer can demand
        # (_require(fs, name)): missing entries surface as
        # NotImplementedError from ordinary library code — matplotlib's
        # savefig calls os.path.realpath, which monkeyfs routes here
        "realpath",
        "resolve_path",
        "getsize",
        "samefile",
        "lexists",
        "islink",
        "readlink",
        "link",
        "symlink",
        "rmdir",
        "replace",
        "access",
        "truncate",
        "utime",
        "chmod",
        "chown",
    }
)


def fs_rpc_handler(fs: Any):
    """Parent-side RPC handler wrapping a real filesystem.

    Content moves as whole blobs (``read``/``write`` in bytes);
    metadata operations dispatch by allowlisted name. Prefers the
    filesystem's own ``read``/``write`` conveniences (monkeyfs
    implementations have them) and falls back to ``open``.
    """

    def _read(path: str) -> bytes:
        read = getattr(fs, "read", None)
        if callable(read):
            return bytes(read(path))
        with fs.open(path, "rb") as f:
            return f.read()

    def _write(path: str, data: bytes) -> None:
        write = getattr(fs, "write", None)
        if callable(write):
            write(path, data)
            return
        with fs.open(path, "wb") as f:
            f.write(data)

    def handler(method: str, args: tuple, kwargs: dict) -> Any:
        if method == "read":
            return _read(*args)
        if method == "write":
            return _write(*args)
        if method in _FS_METHODS:
            return getattr(fs, method)(*args, **kwargs)
        raise AttributeError(f"fs rpc: unsupported method {method!r}")

    return handler


class RemoteFS:
    """Worker-side ``monkeyfs.FileSystem`` over an RPC proxy."""

    def __init__(self, proxy: Any) -> None:
        self._proxy = proxy

    # -- content -----------------------------------------------------

    def read(self, path: str) -> bytes:
        return self._proxy._call("read", path)

    def write(self, path: str, data: Any) -> None:
        if isinstance(data, str):
            data = data.encode()
        self._proxy._call("write", path, bytes(data))

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        binary = "b" in mode
        base = mode.replace("b", "").replace("t", "")
        if base not in ("r", "w", "a", "x", "r+", "w+", "a+", "x+", "+r", "+w", "+a"):
            raise ValueError(f"invalid mode: {mode!r}")
        plus = "+" in base
        kind = base.replace("+", "") or "r"

        if kind == "x" and self.exists(path):
            raise FileExistsError(f"File exists: '{path}'")

        if kind == "r":
            initial = self.read(path)  # missing file raises here
        elif kind == "a" and self.exists(path):
            initial = self.read(path)
        else:  # w, x, missing-file a
            initial = b""

        readable = kind == "r" or plus
        writable = kind != "r" or plus

        encoding = kwargs.get("encoding") or "utf-8"
        errors = kwargs.get("errors") or "strict"
        if binary:
            f: Any = _RemoteBytesFile(self, path, initial, readable, writable)
        else:
            f = _RemoteTextFile(
                self,
                path,
                initial.decode(encoding, errors),
                readable,
                writable,
                encoding,
                errors,
            )
        if kind == "a":
            f.seek(0, io.SEEK_END)
        return f

    # -- metadata (straight RPC) ---------------------------------------

    def stat(self, path: str) -> Any:
        return self._proxy._call("stat", path)

    def exists(self, path: str) -> bool:
        return self._proxy._call("exists", path)

    def isfile(self, path: str) -> bool:
        return self._proxy._call("isfile", path)

    def isdir(self, path: str) -> bool:
        return self._proxy._call("isdir", path)

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        return self._proxy._call("list", path, recursive=recursive)

    def remove(self, path: str) -> None:
        self._proxy._call("remove", path)

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        self._proxy._call("mkdir", path, parents=parents, exist_ok=exist_ok)

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        self._proxy._call("makedirs", path, exist_ok=exist_ok)

    def rename(self, src: str, dst: str) -> None:
        self._proxy._call("rename", src, dst)

    def getcwd(self) -> str:
        return self._proxy._call("getcwd")

    def chdir(self, path: str) -> None:
        self._proxy._call("chdir", path)

    # -- the rest of the monkeyfs surface (straight RPC): anything the
    # -- patch layer can _require() must cross the boundary, or plain
    # -- library code breaks (matplotlib savefig -> os.path.realpath)

    def realpath(self, path: str) -> str:
        return self._proxy._call("realpath", path)

    def resolve_path(self, path: str) -> str:
        return self._proxy._call("resolve_path", path)

    def getsize(self, path: str) -> int:
        return self._proxy._call("getsize", path)

    def samefile(self, path1: str, path2: str) -> bool:
        return self._proxy._call("samefile", path1, path2)

    def lexists(self, path: str) -> bool:
        return self._proxy._call("lexists", path)

    def islink(self, path: str) -> bool:
        return self._proxy._call("islink", path)

    def readlink(self, path: str) -> str:
        return self._proxy._call("readlink", path)

    def link(self, src: str, dst: str) -> None:
        self._proxy._call("link", src, dst)

    def symlink(self, src: str, dst: str) -> None:
        self._proxy._call("symlink", src, dst)

    def rmdir(self, path: str) -> None:
        self._proxy._call("rmdir", path)

    def replace(self, src: str, dst: str) -> None:
        self._proxy._call("replace", src, dst)

    def access(self, path: str, mode: int) -> bool:
        return self._proxy._call("access", path, mode)

    def truncate(self, path: str, length: int) -> None:
        self._proxy._call("truncate", path, length)

    def utime(self, path: str, times: Any = None) -> None:
        self._proxy._call("utime", path, times)

    def chmod(self, path: str, mode: int) -> None:
        self._proxy._call("chmod", path, mode)

    def chown(self, path: str, uid: int, gid: int) -> None:
        self._proxy._call("chown", path, uid, gid)

    def __repr__(self) -> str:
        return "RemoteFS()"


class _PushOnClose:
    """Mixin over an io buffer: push the whole buffer to the parent on
    flush/close (write modes), and gate read/write by mode."""

    _fs: RemoteFS
    _path: str
    _readable: bool
    _writable: bool

    def _push(self) -> None:
        raise NotImplementedError

    def _guard_read(self) -> None:
        if not self._readable:
            raise io.UnsupportedOperation("not readable")

    def _guard_write(self) -> None:
        if not self._writable:
            raise io.UnsupportedOperation("not writable")

    def readable(self) -> bool:  # type: ignore[override]
        return self._readable

    def writable(self) -> bool:  # type: ignore[override]
        return self._writable

    def flush(self) -> None:  # type: ignore[override]
        super().flush()  # type: ignore[misc]
        if self._writable and not self.closed:  # type: ignore[attr-defined]
            self._push()

    def close(self) -> None:  # type: ignore[override]
        if not self.closed and self._writable:  # type: ignore[attr-defined]
            self._push()
        super().close()  # type: ignore[misc]

    def __reduce__(self):
        """Bound to the worker's RPC connection — can't cross the
        process boundary. Raising lets ``filter_namespace`` drop
        leaked handles cleanly (same convention as ``RpcProxy``)."""
        import pickle

        raise pickle.PicklingError(
            "remote file handles are bound to their worker connection"
        )


class _RemoteBytesFile(_PushOnClose, io.BytesIO):
    def __init__(
        self,
        fs: RemoteFS,
        path: str,
        initial: bytes,
        readable: bool,
        writable: bool,
    ) -> None:
        super().__init__(initial)
        self._fs = fs
        self._path = path
        self._readable = readable
        self._writable = writable

    def _push(self) -> None:
        self._fs.write(self._path, self.getvalue())

    def read(self, *args: Any) -> bytes:  # type: ignore[override]
        self._guard_read()
        return super().read(*args)

    def readline(self, *args: Any) -> bytes:  # type: ignore[override]
        self._guard_read()
        return super().readline(*args)

    def write(self, data: Any) -> int:  # type: ignore[override]
        self._guard_write()
        return super().write(data)


class _RemoteTextFile(_PushOnClose, io.StringIO):
    def __init__(
        self,
        fs: RemoteFS,
        path: str,
        initial: str,
        readable: bool,
        writable: bool,
        encoding: str,
        errors: str,
    ) -> None:
        super().__init__(initial)
        self._fs = fs
        self._path = path
        self._readable = readable
        self._writable = writable
        self._encoding = encoding
        self._errors = errors

    def _push(self) -> None:
        self._fs._proxy._call(
            "write", self._path, self.getvalue().encode(self._encoding, self._errors)
        )

    def read(self, *args: Any) -> str:  # type: ignore[override]
        self._guard_read()
        return super().read(*args)

    def readline(self, *args: Any) -> str:  # type: ignore[override]
        self._guard_read()
        return super().readline(*args)

    def write(self, data: str) -> int:  # type: ignore[override]
        self._guard_write()
        return super().write(data)
