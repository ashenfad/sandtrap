"""ContextVar-based filesystem interception control."""

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

current_fs: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "sblite_current_fs", default=None
)


@contextmanager
def use_fs(fs: Any) -> Iterator[None]:
    """Set the active filesystem for the current context."""
    token = current_fs.set(fs)
    try:
        yield
    finally:
        current_fs.reset(token)


@contextmanager
def suspend_fs_interception() -> Iterator[None]:
    """Temporarily disable filesystem interception (for host FS access)."""
    token = current_fs.set(None)
    try:
        yield
    finally:
        current_fs.reset(token)
