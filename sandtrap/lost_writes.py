"""Writes lost when a file is closed implicitly, charged to the execution
that lost them.

A file left for garbage collection saves its content from a finalizer.
A save that fails there is raised out of the finalizer as a lost-write
error, which Python hands to ``sys.unraisablehook``: monkeyfs's
``LostWriteError`` for a file buffered in-process, and :class:`StLostWrite`
for a process worker's file over the parent's filesystem.

One process-wide hook takes them. It routes each one through a ContextVar
to the collector of the execution whose context ran the finalizer, so
concurrent executions on other threads or tasks never see each other's
losses. Everything else, and a lost write with no execution open to take
it, goes on to the hook that was installed before, unchanged.
"""

from __future__ import annotations

import contextvars
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import monkeyfs

from .errors import StLostWrite, lost_write_error, lost_write_note

#: The exception types that report a lost write. monkeyfs releases
#: without ``LostWriteError`` discard a failed in-process save instead,
#: so only the process worker's is seen there.
LOST_WRITE_TYPES: tuple[type[BaseException], ...] = (StLostWrite,) + (
    (monkeyfs.LostWriteError,) if hasattr(monkeyfs, "LostWriteError") else ()
)


class LostWrites:
    """One execution's lost writes, as ``(path, cause)`` pairs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open = True
        self._lost: list[tuple[str, BaseException]] = []

    def offer(self, path: str, cause: BaseException) -> bool:
        """Record a lost write, unless the execution has ended."""
        with self._lock:
            if self._open:
                self._lost.append((path, cause))
            return self._open

    def close(self) -> list[tuple[str, BaseException]]:
        """End collection and return what was lost."""
        with self._lock:
            self._open = False
            return list(self._lost)


_current: contextvars.ContextVar[LostWrites | None] = contextvars.ContextVar(
    "sandtrap_lost_writes", default=None
)
_install_lock = threading.Lock()


def _make_hook(previous: Any) -> Any:
    def hook(unraisable: Any) -> None:
        exc = unraisable.exc_value
        collector = _current.get()
        if collector is not None and isinstance(exc, LOST_WRITE_TYPES):
            cause = exc.__cause__ or exc
            # The finalizer's frames hold the file and its buffered
            # content; the error outlives them only by its message and
            # the frames' names.
            for tb in (exc.__traceback__, cause.__traceback__):
                if tb is not None:
                    traceback.clear_frames(tb)
            if collector.offer(str(exc.filename), cause):
                return
        previous(unraisable)

    hook._sandtrap_lost_writes = True  # type: ignore[attr-defined]
    return hook


def install() -> None:
    """Put the lost-write hook in front of ``sys.unraisablehook``.

    Done per execution rather than once, so a hook something else
    installed in the meantime (a test runner's, say) still sees what this
    one passes on: it is wrapped, never replaced. A hook already in front
    is left alone.
    """
    with _install_lock:
        if not getattr(sys.unraisablehook, "_sandtrap_lost_writes", False):
            sys.unraisablehook = _make_hook(sys.unraisablehook)


@contextmanager
def collecting() -> Iterator[LostWrites]:
    """Charge lost writes finalized in this context to a new collector.

    Tasks started from the context copy it, as do threads while the
    sandbox's context propagation is installed, so their lost writes are
    charged here too. Once the block exits the collector
    takes no more, and a later loss goes to the previous hook.
    """
    install()
    collector = LostWrites()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)
        collector.close()


def settle(
    error: BaseException | None,
    lost: list[tuple[str, BaseException]],
    stderr: Any,
) -> BaseException | None:
    """The execution's error, once its lost writes are accounted for.

    An execution that lost a write fails: with :class:`StLostWrite` for
    the first one if it had no error of its own, and with a note on its
    error for every other. Python 3.10 has no ``add_note``, so there the
    note is appended to ``__notes__`` as 3.11 would, and also written to
    *stderr*, since 3.10's tracebacks don't show it. So is a note the
    error won't take.
    """
    if not lost:
        return error
    if error is None:
        path, cause = lost[0]
        error = lost_write_error(path, cause)
        lost = lost[1:]
    for path, cause in lost:
        note = lost_write_note(path, cause)
        try:
            if hasattr(error, "add_note"):
                error.add_note(note)
                shown = True
            else:
                notes = getattr(error, "__notes__", None)
                if not isinstance(notes, list):
                    notes = []
                    error.__notes__ = notes  # type: ignore[attr-defined]
                notes.append(note)
                shown = False
        except Exception:
            shown = False  # an error that takes no attributes
        if not shown:
            stderr.write(note + "\n")
    return error
