"""Installation of network and threading patches.

Patches are installed once and remain active for the process lifetime.
They are inert when network access is allowed (``network_allowed`` is
``True``, the default), falling through to the original methods.

Threading patches ensure that ``contextvars`` state (including
``network_allowed``) propagates to worker threads spawned via
``threading.Thread``, ``ThreadPoolExecutor.submit``, and
``ThreadPoolExecutor.map``.
"""

import concurrent.futures
import contextvars
import socket
import threading
from typing import Any

from . import socket as _gated

_lock = threading.Lock()
_installed = False

_METHODS = [
    "connect",
    "connect_ex",
    "bind",
    "listen",
    "accept",
    "send",
    "sendall",
    "sendto",
    "sendfile",
    "recv",
    "recvfrom",
    "recv_into",
    "recvfrom_into",
]

_PATCHES = {
    "connect": _gated._p_connect,
    "connect_ex": _gated._p_connect_ex,
    "bind": _gated._p_bind,
    "listen": _gated._p_listen,
    "accept": _gated._p_accept,
    "send": _gated._p_send,
    "sendall": _gated._p_sendall,
    "sendto": _gated._p_sendto,
    "sendfile": _gated._p_sendfile,
    "recv": _gated._p_recv,
    "recvfrom": _gated._p_recvfrom,
    "recv_into": _gated._p_recv_into,
    "recvfrom_into": _gated._p_recvfrom_into,
}

# --- Threading originals ---
_original_thread_start: Any = None
_original_executor_submit: Any = None
_original_executor_map: Any = None


# --- Threading patches ---


def _p_thread_start(self: Any) -> Any:
    """Patched Thread.start() that propagates contextvars to the new thread."""
    ctx = contextvars.copy_context()
    original_run = self.run

    def _ctx_run(*args: Any, **kwargs: Any) -> Any:
        return ctx.run(original_run, *args, **kwargs)

    self.run = _ctx_run
    return _original_thread_start(self)


def _p_executor_submit(self: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Patched ThreadPoolExecutor.submit() that propagates contextvars."""
    ctx = contextvars.copy_context()
    return _original_executor_submit(self, ctx.run, fn, *args, **kwargs)


def _p_executor_map(self: Any, fn: Any, *iterables: Any, **kwargs: Any) -> Any:
    """Patched ThreadPoolExecutor.map() that propagates contextvars."""
    ctx = contextvars.copy_context()

    def _ctx_fn(*args: Any) -> Any:
        return ctx.run(fn, *args)

    return _original_executor_map(self, _ctx_fn, *iterables, **kwargs)


def install() -> None:
    """Install network and threading patches (idempotent, permanent)."""
    global _installed, _original_thread_start
    global _original_executor_submit, _original_executor_map
    with _lock:
        if _installed:
            return

        # Store originals — sockets
        for name in _METHODS:
            _gated._originals[name] = getattr(socket.socket, name)
        _gated._original_getaddrinfo = socket.getaddrinfo

        # Install patches on socket.socket methods
        for name, patch_fn in _PATCHES.items():
            setattr(socket.socket, name, patch_fn)

        # Install getaddrinfo patch at module level
        socket.getaddrinfo = _gated._p_getaddrinfo

        # Store originals — threading
        _original_thread_start = threading.Thread.start
        _original_executor_submit = concurrent.futures.ThreadPoolExecutor.submit
        _original_executor_map = concurrent.futures.ThreadPoolExecutor.map

        # Install threading patches
        threading.Thread.start = _p_thread_start  # type: ignore[assignment]
        concurrent.futures.ThreadPoolExecutor.submit = _p_executor_submit  # type: ignore[assignment]
        concurrent.futures.ThreadPoolExecutor.map = _p_executor_map  # type: ignore[method-assign]

        _installed = True
