"""Where sandboxed code lives: a module of its own, in ``sys.modules``.

Code a :class:`~sandtrap.Sandbox` runs executes in a module named for
that sandbox (``__sandtrap_<n>__``), registered in ``sys.modules`` while
the sandbox is in use. A class the code defines names that module as
its ``__module__``, so what Python finds a class's home by finds it:
``dataclasses`` reading a quoted annotation, ``typing.get_type_hints``,
``inspect.getmodule``.

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
import types
from typing import Any

__all__ = ["PREFIX", "SandboxObjectError", "check_sendable", "is_sandbox_module"]

PREFIX = "__sandtrap_"
"""What every sandbox module's name starts with."""

_count = itertools.count(1)


def new_module_name() -> str:
    """A name for one sandbox's module, unique in this process."""
    return f"{PREFIX}{next(_count)}__"


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
