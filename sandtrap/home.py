"""Where sandboxed code lives: a module of its own, in ``sys.modules``.

Each execution of a :class:`~sandtrap.Sandbox` runs its code in a module
of its own (``__sandtrap_<sandbox>_<execution>__``), registered in
``sys.modules`` for the sandbox's latest :data:`HOMES` executions while
it is in use. A class the code defines names that module as its
``__module__``, so what Python finds a class's home by finds it:
``dataclasses`` reading a quoted annotation, ``typing.get_type_hints``,
``inspect.getmodule``. A module per execution means a class never
resolves against another execution's names.

It also makes the code's own classes and functions picklable by
reference, where before nothing could find them. Pickled data has to be
read back where that module exists, which outside this process (the
parent of a worker, a store read later) it doesn't, and reading it back
there would run the code's own class outside the sandbox. So whatever
leaves a sandbox by pickle is checked first (:func:`check_sendable`):
a class or function of the sandbox's own is refused, as it was before
by pickle itself.
"""

from __future__ import annotations

import io
import itertools
import pickle
import re
import types
from typing import Any

__all__ = [
    "HOMES",
    "PREFIX",
    "SandboxObjectError",
    "check_sendable",
    "dump_sendable",
    "is_sandbox_module",
]

PREFIX = "__sandtrap_"
"""What every sandbox module's name starts with."""

HOMES = 8
"""How many of a sandbox's executions keep their module registered: the
latest ones. A class from an older one loses its home (what Python finds
it by), and its namespace is let go; all of them go when the sandbox
exits."""

_count = itertools.count(1)


def new_module_name() -> str:
    """A name for one sandbox's modules, unique in this process: each
    execution's module is this name with the execution's number."""
    return f"{PREFIX}{next(_count)}__"


def forget(names: Any) -> None:
    """Take the modules named ``names`` out of ``sys.modules``, and
    empty ``names``."""
    import sys

    while names:
        sys.modules.pop(names.pop(), None)


def is_sandbox_module(name: Any) -> bool:
    """Whether ``name`` (a ``__module__``) is a sandbox's module: what a
    class or function defined by sandboxed code says it belongs to."""
    return isinstance(name, str) and name.startswith(PREFIX) and name.endswith("__")


class SandboxObjectError(pickle.PicklingError):
    """A class or function the sandboxed code defined, refused where a
    value leaves the sandbox by pickle."""


class _Refusing(pickle.Pickler):
    def reducer_override(self, obj: Any) -> Any:
        if isinstance(obj, (type, types.FunctionType)) and is_sandbox_module(
            getattr(obj, "__module__", None)
        ):
            kind = "class" if isinstance(obj, type) else "function"
            raise SandboxObjectError(
                f"{kind} {obj.__qualname__!r} is defined in the sandboxed code, "
                "and can't leave it: send its data instead (a dict, a list, or "
                "a type the host provides)"
            )
        return NotImplemented


def check_sendable(obj: Any) -> bytes:
    """``obj`` pickled, or :class:`SandboxObjectError` when it holds a
    class or function of a sandbox's own (or a value of one). Any other
    pickling error is pickle's, raised as it is."""
    raw = pickle.dumps(obj)
    if PREFIX.encode() in raw:  # by reference, the name is in the bytes
        _Refusing(io.BytesIO()).dump(obj)
    return raw


class _Discard:
    """A file that keeps nothing: what the refusing pickler writes to when
    only its refusal is wanted."""

    def write(self, data: Any) -> int:
        return len(memoryview(data))


_PREFIX_BYTES = PREFIX.encode()
_PREFIX_RE = re.compile(re.escape(_PREFIX_BYTES))


class _Scanning:
    """Passes writes through to ``file``, noting whether the bytes written
    contain a sandbox module's name, a match split across two writes
    included. ``re`` reads any buffer in place, so nothing is copied."""

    def __init__(self, file: Any) -> None:
        self._file = file
        self._tail = b""
        self.found = False

    def write(self, data: Any) -> int:
        view = memoryview(data).cast("B")
        if not self.found:
            keep = len(_PREFIX_BYTES) - 1
            if _PREFIX_RE.search(self._tail + bytes(view[:keep])) or (
                _PREFIX_RE.search(view)
            ):
                self.found = True
            self._tail = (
                bytes(view[-keep:])
                if len(view) >= keep
                else (self._tail + bytes(view))[-keep:]
            )
        return self._file.write(view)


def dump_sendable(obj: Any, file: Any) -> None:
    """Pickle ``obj`` to ``file`` as :func:`check_sendable` checks it, a
    piece at a time rather than as one ``bytes``. Uses the highest
    protocol, under which a large buffer (``bytes``, ``bytearray``, an
    array's data) goes to ``file`` as it is; older protocols copy a
    ``bytearray`` into a ``bytes`` first. Raises :class:`SandboxObjectError`
    when ``obj`` holds a class or function of a sandbox's own, after part
    of the pickle may already have been written; the caller discards it."""
    scanning = _Scanning(file)
    pickle.dump(obj, scanning, protocol=pickle.HIGHEST_PROTOCOL)
    if scanning.found:  # by reference, the name is in the bytes
        _Refusing(_Discard(), protocol=pickle.HIGHEST_PROTOCOL).dump(obj)
