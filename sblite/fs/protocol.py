"""Abstract filesystem protocol for sandbox interception."""

from abc import ABC, abstractmethod
from typing import Any


class FileSystem(ABC):
    """Abstract base class for sandbox filesystem implementations.

    Hosts provide a concrete subclass to intercept file I/O
    during sandboxed execution.
    """

    @abstractmethod
    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """Open a file and return a file-like object."""
        ...

    @abstractmethod
    def stat(self, path: str) -> Any:
        """Return an os.stat_result-like object for the path."""
        ...

    @abstractmethod
    def listdir(self, path: str) -> list[str]:
        """List directory contents."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True if the path exists."""
        ...

    @abstractmethod
    def isfile(self, path: str) -> bool:
        """Return True if the path is a regular file."""
        ...

    @abstractmethod
    def isdir(self, path: str) -> bool:
        """Return True if the path is a directory."""
        ...

    @abstractmethod
    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        ...

    @abstractmethod
    def makedirs(self, path: str, *, exist_ok: bool = False) -> None:
        """Create a directory tree."""
        ...

    @abstractmethod
    def remove(self, path: str) -> None:
        """Remove a file."""
        ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None:
        """Rename a file or directory."""
        ...

    @abstractmethod
    def getcwd(self) -> str:
        """Return the current working directory."""
        ...

    @abstractmethod
    def chdir(self, path: str) -> None:
        """Change the current working directory."""
        ...
