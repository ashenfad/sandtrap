"""Thread-safe installation of network socket patches."""

import socket
import threading

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


def install() -> None:
    """Install network socket patches (idempotent, thread-safe)."""
    global _installed
    with _lock:
        if _installed:
            return

        # Store originals
        for name in _METHODS:
            _gated._originals[name] = getattr(socket.socket, name)
        _gated._original_getaddrinfo = socket.getaddrinfo

        # Install patches on socket.socket methods
        for name, patch_fn in _PATCHES.items():
            setattr(socket.socket, name, patch_fn)

        # Install getaddrinfo patch at module level
        socket.getaddrinfo = _gated._p_getaddrinfo

        _installed = True


def uninstall() -> None:
    """Uninstall network socket patches (idempotent, thread-safe)."""
    global _installed
    with _lock:
        if not _installed:
            return

        for name in _METHODS:
            setattr(socket.socket, name, _gated._originals[name])
        socket.getaddrinfo = _gated._original_getaddrinfo

        _gated._originals.clear()
        _gated._original_getaddrinfo = None
        _installed = False
