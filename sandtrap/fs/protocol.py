"""Filesystem protocol for sandbox interception."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FileSystem(Protocol):
    """Protocol for sandbox filesystem implementations.

    Hosts provide an object satisfying this protocol to intercept file I/O
    during sandboxed execution.  Any object with these methods works —
    no subclassing required.
    """

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """Open a file and return a file-like object."""
        ...

    def stat(self, path: str) -> Any:
        """Return an os.stat_result-like object for the path.

        The returned object should have ``st_size``, ``st_mode``,
        ``st_mtime``, ``st_ctime``, and ``st_atime`` attributes for
        compatibility with code that uses ``os.stat()``.
        """
        ...

    def listdir(self, path: str) -> list[str]:
        """List directory contents."""
        ...

    def exists(self, path: str) -> bool:
        """Return True if the path exists."""
        ...

    def isfile(self, path: str) -> bool:
        """Return True if the path is a regular file."""
        ...

    def isdir(self, path: str) -> bool:
        """Return True if the path is a directory."""
        ...

    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        ...

    def makedirs(self, path: str, *, exist_ok: bool = False) -> None:
        """Create a directory tree."""
        ...

    def remove(self, path: str) -> None:
        """Remove a file."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Rename a file or directory."""
        ...

    def getcwd(self) -> str:
        """Return the current working directory."""
        ...

    def chdir(self, path: str) -> None:
        """Change the current working directory."""
        ...
