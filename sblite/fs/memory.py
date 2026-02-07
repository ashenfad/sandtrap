"""In-memory filesystem implementation."""

import os
import posixpath
import stat as stat_mod
from io import BytesIO, StringIO
from typing import Any

from .protocol import FileSystem


class MemoryFS(FileSystem):
    """Simple in-memory filesystem.

    Useful for testing and as a lightweight VFS for sandboxed agents
    that need to create and import local modules.
    """

    def __init__(self) -> None:
        self.files: dict[str, str | bytes] = {}
        self.dirs: set[str] = {"/"}
        self._cwd = "/"

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        path = self._resolve(path)
        if "r" in mode:
            if path not in self.files:
                raise FileNotFoundError(f"No such file: '{path}'")
            content = self.files[path]
            if "b" in mode:
                if isinstance(content, str):
                    content = content.encode("utf-8")
                return BytesIO(content)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            return StringIO(content)
        elif "w" in mode or "a" in mode:
            parent = posixpath.dirname(path)
            if parent != path and parent not in self.dirs:
                raise FileNotFoundError(f"No such directory: '{parent}'")
            is_binary = "b" in mode
            buf = BytesIO() if is_binary else StringIO()
            if "a" in mode and path in self.files:
                existing = self.files[path]
                if is_binary:
                    buf.write(existing if isinstance(existing, bytes) else existing.encode("utf-8"))
                else:
                    buf.write(existing if isinstance(existing, str) else existing.decode("utf-8"))
            original_close = buf.close

            def close_and_save() -> None:
                self.files[path] = buf.getvalue()
                original_close()

            buf.close = close_and_save  # type: ignore[method-assign]
            return buf
        raise ValueError(f"Unsupported mode: {mode}")

    def stat(self, path: str) -> Any:
        path = self._resolve(path)
        if path in self.files:
            content = self.files[path]
            size = len(content.encode("utf-8") if isinstance(content, str) else content)
            return os.stat_result((
                stat_mod.S_IFREG | 0o644,
                0, 0, 1, 1000, 1000,
                size,
                0, 0, 0,
            ))
        if path in self.dirs:
            return os.stat_result((
                stat_mod.S_IFDIR | 0o755,
                0, 0, 2, 1000, 1000,
                0, 0, 0, 0,
            ))
        raise FileNotFoundError(f"No such file or directory: '{path}'")

    def listdir(self, path: str) -> list[str]:
        path = self._resolve(path)
        if path not in self.dirs:
            raise FileNotFoundError(f"No such directory: '{path}'")
        prefix = path.rstrip("/") + "/"
        entries: set[str] = set()
        for f in self.files:
            if f.startswith(prefix):
                rest = f[len(prefix):]
                entries.add(rest.split("/")[0])
        for d in self.dirs:
            if d.startswith(prefix) and d != path:
                rest = d[len(prefix):]
                if rest:
                    entries.add(rest.split("/")[0])
        return sorted(entries)

    def exists(self, path: str) -> bool:
        path = self._resolve(path)
        return path in self.files or path in self.dirs

    def isfile(self, path: str) -> bool:
        path = self._resolve(path)
        return path in self.files

    def isdir(self, path: str) -> bool:
        path = self._resolve(path)
        return path in self.dirs

    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        path = self._resolve(path)
        if parents:
            self.makedirs(path, exist_ok=exist_ok)
            return
        if path in self.dirs:
            if exist_ok:
                return
            raise FileExistsError(f"Directory exists: '{path}'")
        parent = posixpath.dirname(path)
        if parent != path and parent not in self.dirs:
            raise FileNotFoundError(f"No such directory: '{parent}'")
        self.dirs.add(path)

    def makedirs(self, path: str, *, exist_ok: bool = False) -> None:
        path = self._resolve(path)
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current += "/" + part
            self.mkdir(current, exist_ok=True)

    def remove(self, path: str) -> None:
        path = self._resolve(path)
        if path not in self.files:
            raise FileNotFoundError(f"No such file: '{path}'")
        del self.files[path]

    def rename(self, src: str, dst: str) -> None:
        src = self._resolve(src)
        dst = self._resolve(dst)
        if src in self.files:
            self.files[dst] = self.files.pop(src)
        elif src in self.dirs:
            # Rename the directory and all children (files and subdirs)
            src_prefix = src.rstrip("/") + "/"
            self.dirs.discard(src)
            self.dirs.add(dst)
            for d in list(self.dirs):
                if d.startswith(src_prefix):
                    self.dirs.discard(d)
                    self.dirs.add(dst + d[len(src):])
            for f in list(self.files):
                if f.startswith(src_prefix):
                    self.files[dst + f[len(src):]] = self.files.pop(f)
        else:
            raise FileNotFoundError(f"No such file or directory: '{src}'")

    def getcwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        path = self._resolve(path)
        if path not in self.dirs:
            raise FileNotFoundError(f"No such directory: '{path}'")
        self._cwd = path

    def _resolve(self, path: str) -> str:
        """Resolve a path relative to cwd and normalize . and .. components."""
        if not path.startswith("/"):
            path = self._cwd.rstrip("/") + "/" + path
        return posixpath.normpath(path)
