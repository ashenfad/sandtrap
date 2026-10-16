"""Results spooled to files: a value per file, read when it is wanted.

With spooling on, a worker writes each value of a result namespace to a
file of its own, in a directory the parent created for the sandbox, and
sends only what it wrote: each name's size and file. A value whose
pickle is small rides the pipe instead, so a result of a few numbers
makes no files. The parent reads a value when it is first asked for.

Streaming each value to its own file means neither side holds a second
copy of it: a large buffer goes from the worker's memory to the file as
it is, and the parent's ``pickle.load`` reads it straight into the
value. A value nobody asks for is never read at all, and its size is
known before anything is.
"""

from __future__ import annotations

import io
import os
import pickle
import re
import secrets
import threading
import weakref
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..home import dump_sendable

INLINE_LIMIT = 64 * 1024
"""A value whose pickle is at most this many bytes rides the pipe."""

_FILE_RE = re.compile(r"[0-9a-f]{32}\.pkl")


@dataclass(frozen=True)
class SpoolEntry:
    """One spooled value: its pickled size, and either the file it was
    written to (a name inside the spool directory) or the pickle itself."""

    size: int
    file: str | None = None
    data: bytes | None = None


class SpoolClosedError(RuntimeError):
    """A spooled value was asked for after its sandbox closed, which
    removes the files of every value not yet read."""


class _SpoolWriter:
    """What ``pickle.dump`` writes a value to: memory up to
    :data:`INLINE_LIMIT`, then a new file in ``directory``."""

    def __init__(self, directory: str) -> None:
        self._directory = directory
        self._buffer: io.BytesIO | None = io.BytesIO()
        self._file: Any = None
        self.name: str | None = None
        self.size = 0

    def write(self, data: Any) -> int:
        view = memoryview(data)
        n = view.nbytes
        if self._file is None:
            assert self._buffer is not None
            if self.size + n <= INLINE_LIMIT:
                self._buffer.write(view)
                self.size += n
                return n
            self.name = f"{secrets.token_hex(16)}.pkl"
            path = os.path.join(self._directory, self.name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._file = os.fdopen(fd, "wb")
            self._file.write(self._buffer.getbuffer())
            self._buffer = None
        self._file.write(view)
        self.size += n
        return n

    def finish(self) -> SpoolEntry:
        if self._file is None:
            assert self._buffer is not None
            return SpoolEntry(size=self.size, data=self._buffer.getvalue())
        self._file.close()
        return SpoolEntry(size=self.size, file=self.name)

    def discard(self) -> None:
        if self._file is not None:
            self._file.close()
            assert self.name is not None
            try:
                os.unlink(os.path.join(self._directory, self.name))
            except OSError:
                pass
        self._buffer = None


def spool_namespace(
    ns: Mapping[str, Any], directory: str
) -> tuple[dict[str, SpoolEntry], tuple[str, ...]]:
    """Spool each value of ``ns`` into ``directory``: ``(entries, dropped)``,
    where ``dropped`` names the values that could not be pickled, as
    :func:`~sandtrap.process.protocol.partition_namespace` does. A value
    that fails part-way leaves no file behind."""
    entries: dict[str, SpoolEntry] = {}
    dropped: list[str] = []
    for name, value in ns.items():
        writer = _SpoolWriter(directory)
        try:
            dump_sendable(value, writer)
            entries[name] = writer.finish()
        except Exception:
            # Anything: pickling arbitrary objects raises arbitrary
            # exceptions. Unpicklable means dropped, never fatal.
            writer.discard()
            dropped.append(name)
    return entries, tuple(dropped)


def _remove(directory: str, files: set[str]) -> None:
    for name in list(files):
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass
    files.clear()


class SpooledNamespace(Mapping[str, Any]):
    """A result namespace whose values are read on first access.

    Behaves as the namespace a worker would otherwise return: indexing,
    ``get``, iteration and ``in`` work as on a dict, and only reading a
    value reads its file. ``in`` and iteration read nothing. A value read
    once is kept, and its file removed; the files of values never read
    are removed when this mapping is let go, or when the sandbox closes,
    after which reading one raises :class:`SpoolClosedError`.
    """

    def __init__(self, directory: str, entries: Mapping[str, SpoolEntry]) -> None:
        for name, entry in entries.items():
            # Only a file the worker could have named: a bare name of the
            # expected shape, so nothing outside the directory is read.
            if entry.file is not None and not _FILE_RE.fullmatch(entry.file):
                raise ValueError(f"unexpected spool file name for {name!r}")
        self._directory = directory
        self._entries = dict(entries)
        self._values: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._files = {e.file for e in self._entries.values() if e.file is not None}
        self._finalizer = weakref.finalize(self, _remove, directory, self._files)

    @property
    def sizes(self) -> dict[str, int]:
        """Each value's pickled size in bytes, read without reading it."""
        return {name: entry.size for name, entry in self._entries.items()}

    def __getitem__(self, name: str) -> Any:
        with self._lock:
            if name in self._values:
                return self._values[name]
            entry = self._entries[name]  # KeyError for a name not returned
            value = self._read(name, entry)
            self._values[name] = value
            return value

    def _read(self, name: str, entry: SpoolEntry) -> Any:
        if entry.file is None:
            assert entry.data is not None
            return pickle.loads(entry.data)
        path = os.path.join(self._directory, entry.file)
        try:
            with open(path, "rb") as f:
                value = pickle.load(f)
        except FileNotFoundError:
            raise SpoolClosedError(
                f"{name!r} was never read, and its sandbox has closed, which "
                "removes the files of unread values: read what you need "
                "before the sandbox exits"
            ) from None
        self._files.discard(entry.file)
        try:
            os.unlink(path)
        except OSError:
            pass
        return value

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{name!r}: <{entry.size} bytes{'' if name not in self._values else ', read'}>"
            for name, entry in self._entries.items()
        )
        return f"SpooledNamespace({{{shown}}})"
