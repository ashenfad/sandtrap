"""Installation of filesystem patches.

Patches are installed once and remain active for the process lifetime.
They are inert when no sandbox filesystem is active (``current_fs`` is
``None``), falling through to the original functions transparently.
"""

import builtins
import contextvars
import os
import threading
from typing import Any

from .context import current_fs

_lock = threading.Lock()
_installed = False
_originals: dict[str, Any] = {}

# Recursion guard: prevents re-interception when FS internals do real I/O.
_in_fs_op: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sblite_in_fs_op", default=False
)


def _get_fs() -> Any:
    """Return the active filesystem, or None if bypassed."""
    if _in_fs_op.get():
        return None
    return current_fs.get()


# --- Patched functions ---


def _patched_open(path: Any, *args: Any, **kwargs: Any) -> Any:
    fs = _get_fs()
    if fs is not None:
        mode = args[0] if args else kwargs.get("mode", "r")
        remaining_kwargs = {k: v for k, v in kwargs.items() if k != "mode"}
        token = _in_fs_op.set(True)
        try:
            return fs.open(str(path), mode, **remaining_kwargs)
        finally:
            _in_fs_op.reset(token)
    return _originals["open"](path, *args, **kwargs)


def _patched_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.stat(str(path))
        finally:
            _in_fs_op.reset(token)
    return _originals["stat"](path, *args, **kwargs)


def _patched_lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.stat(str(path))  # Delegate to stat for simplicity
        finally:
            _in_fs_op.reset(token)
    return _originals["lstat"](path, *args, **kwargs)


def _patched_listdir(path: Any = ".") -> list[str]:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.listdir(str(path))
        finally:
            _in_fs_op.reset(token)
    return _originals["listdir"](path)


def _patched_exists(path: Any) -> bool:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.exists(str(path))
        finally:
            _in_fs_op.reset(token)
    return _originals["exists"](path)


def _patched_isfile(path: Any) -> bool:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.isfile(str(path))
        finally:
            _in_fs_op.reset(token)
    return _originals["isfile"](path)


def _patched_isdir(path: Any) -> bool:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.isdir(str(path))
        finally:
            _in_fs_op.reset(token)
    return _originals["isdir"](path)


def _patched_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            fs.mkdir(str(path), *args, **kwargs)
            return
        finally:
            _in_fs_op.reset(token)
    return _originals["mkdir"](path, *args, **kwargs)


def _patched_makedirs(path: Any, *args: Any, **kwargs: Any) -> None:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            exist_ok = kwargs.get("exist_ok", False)
            fs.makedirs(str(path), exist_ok=exist_ok)
            return
        finally:
            _in_fs_op.reset(token)
    return _originals["makedirs"](path, *args, **kwargs)


def _patched_remove(path: Any) -> None:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            fs.remove(str(path))
            return
        finally:
            _in_fs_op.reset(token)
    return _originals["remove"](path)


def _patched_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            fs.remove(str(path))
            return
        finally:
            _in_fs_op.reset(token)
    return _originals["unlink"](path, *args, **kwargs)


def _patched_rename(src: Any, dst: Any) -> None:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            fs.rename(str(src), str(dst))
            return
        finally:
            _in_fs_op.reset(token)
    return _originals["rename"](src, dst)


def _patched_getcwd() -> str:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            return fs.getcwd()
        finally:
            _in_fs_op.reset(token)
    return _originals["getcwd"]()


def _patched_chdir(path: Any) -> None:
    fs = _get_fs()
    if fs is not None:
        token = _in_fs_op.set(True)
        try:
            fs.chdir(str(path))
            return
        finally:
            _in_fs_op.reset(token)
    return _originals["chdir"](path)


def install() -> None:
    """Install filesystem patches (idempotent, permanent)."""
    global _installed
    with _lock:
        if _installed:
            return

        # Store originals
        _originals["open"] = builtins.open
        _originals["stat"] = os.stat
        _originals["lstat"] = os.lstat
        _originals["listdir"] = os.listdir
        _originals["exists"] = os.path.exists
        _originals["isfile"] = os.path.isfile
        _originals["isdir"] = os.path.isdir
        _originals["mkdir"] = os.mkdir
        _originals["makedirs"] = os.makedirs
        _originals["remove"] = os.remove
        _originals["unlink"] = os.unlink
        _originals["rename"] = os.rename
        _originals["getcwd"] = os.getcwd
        _originals["chdir"] = os.chdir

        # Install patches
        builtins.open = _patched_open
        os.stat = _patched_stat
        os.lstat = _patched_lstat
        os.listdir = _patched_listdir
        os.path.exists = _patched_exists
        os.path.isfile = _patched_isfile
        os.path.isdir = _patched_isdir
        os.mkdir = _patched_mkdir
        os.makedirs = _patched_makedirs
        os.remove = _patched_remove
        os.unlink = _patched_unlink
        os.rename = _patched_rename
        os.getcwd = _patched_getcwd
        os.chdir = _patched_chdir

        _installed = True
