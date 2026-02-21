"""ContextVar-based network permission control."""

import contextvars
from contextlib import contextmanager
from typing import Iterator

network_allowed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sandtrap_network_allowed", default=True
)


@contextmanager
def deny_network() -> Iterator[None]:
    """Deny network access in the current context."""
    token = network_allowed.set(False)
    try:
        yield
    finally:
        network_allowed.reset(token)


@contextmanager
def allow_network() -> Iterator[None]:
    """Temporarily allow network access in the current context."""
    token = network_allowed.set(True)
    try:
        yield
    finally:
        network_allowed.reset(token)
