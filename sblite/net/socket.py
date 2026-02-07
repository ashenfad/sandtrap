"""Socket method patching for network interception."""

import socket as _socket
from typing import Any

from .context import network_allowed

_NETWORK_FAMILIES = {_socket.AF_INET, _socket.AF_INET6}

# Stores original unpatched methods/functions.
_originals: dict[str, Any] = {}
_original_getaddrinfo: Any = None


def _is_network_socket(sock: _socket.socket) -> bool:
    """Check if a socket is a network socket (vs local/unix)."""
    try:
        return sock.family in _NETWORK_FAMILIES
    except Exception:
        return True  # Conservative: block if can't determine


def _check_network(operation: str, sock: _socket.socket | None = None) -> None:
    """Raise if network access is not allowed in the current context."""
    # AF_UNIX and other local sockets always pass through.
    if sock is not None and not _is_network_socket(sock):
        return
    if not network_allowed.get():
        from ..errors import SbError

        raise SbError(
            f"Network access denied: {operation}() blocked by sandbox. "
            f"Register with network_access=True to allow."
        )


# --- Patched socket instance methods ---


def _p_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("connect", self)
    return _originals["connect"](self, *args, **kwargs)


def _p_connect_ex(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("connect_ex", self)
    return _originals["connect_ex"](self, *args, **kwargs)


def _p_bind(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("bind", self)
    return _originals["bind"](self, *args, **kwargs)


def _p_listen(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("listen", self)
    return _originals["listen"](self, *args, **kwargs)


def _p_accept(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("accept", self)
    return _originals["accept"](self, *args, **kwargs)


def _p_send(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("send", self)
    return _originals["send"](self, *args, **kwargs)


def _p_sendall(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("sendall", self)
    return _originals["sendall"](self, *args, **kwargs)


def _p_sendto(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("sendto", self)
    return _originals["sendto"](self, *args, **kwargs)


def _p_sendfile(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("sendfile", self)
    return _originals["sendfile"](self, *args, **kwargs)


def _p_recv(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("recv", self)
    return _originals["recv"](self, *args, **kwargs)


def _p_recvfrom(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("recvfrom", self)
    return _originals["recvfrom"](self, *args, **kwargs)


def _p_recv_into(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("recv_into", self)
    return _originals["recv_into"](self, *args, **kwargs)


def _p_recvfrom_into(self: Any, *args: Any, **kwargs: Any) -> Any:
    _check_network("recvfrom_into", self)
    return _originals["recvfrom_into"](self, *args, **kwargs)


# --- Patched module-level function ---


def _p_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
    _check_network("getaddrinfo")
    return _original_getaddrinfo(*args, **kwargs)
