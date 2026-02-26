"""macOS Seatbelt (sandbox-exec) process sandboxing.

Uses the (deprecated but functional) ``sandbox_init_with_parameters``
API via ctypes to restrict the current process's filesystem and network
access.  Graceful no-op if the API is unavailable.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import warnings

# ---------------------------------------------------------------------------
# SBPL profile fragments — assembled conditionally by apply().
# ---------------------------------------------------------------------------

_HEADER = """\
(version 1)
(deny default)
(import "bsd.sb")
"""

# Filesystem: restricted to sandbox root + system read-only paths.
_FS_RESTRICTED = """\
(define sandbox-root (param "SANDBOX_ROOT"))

; Full file access within the sandbox directory
(allow file-read* file-write*
  (subpath sandbox-root))

; Read-only access to system paths the Python runtime needs
(allow file-read*
  (subpath "/usr")
  (subpath "/System/Library")
  (subpath "/Library")
  (literal "/private/etc/localtime")
  (literal "/dev/null")
  (literal "/dev/urandom")
  (literal "/dev/random")
  (literal "/dev/fd"))

; Allow process management (Python threads)
(allow process-exec
  (subpath "/usr"))
"""

# Filesystem: unrestricted (policy has host_fs_access registrations).
_FS_OPEN = """\
(allow file-read* file-write*)
(allow process-exec)
"""

_NETWORK_DENY = """\
; Block network
(deny network*)
"""

_NETWORK_ALLOW = """\
; Allow network
(allow network*)
"""


def available() -> bool:
    """Return True if Seatbelt can be applied on this system."""
    if sys.platform != "darwin":
        return False
    try:
        lib = ctypes.CDLL("libSystem.dylib")
        return hasattr(lib, "sandbox_init_with_parameters")
    except OSError:
        return False


def apply(
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> bool:
    """Apply a Seatbelt profile.

    Parameters
    ----------
    root:
        Absolute path to the sandbox filesystem root, or None to skip
        filesystem restriction.
    allow_network:
        If True, allow network operations through the Seatbelt profile.
    allow_host_fs:
        If True, do not restrict filesystem access.

    Returns True if the profile was applied, False if unavailable.
    """
    if sys.platform != "darwin":
        return False

    try:
        lib = ctypes.CDLL("libSystem.dylib")
    except OSError:
        return False

    if not hasattr(lib, "sandbox_init_with_parameters"):
        return False

    # Assemble profile from fragments
    restrict_fs = root is not None and not allow_host_fs
    profile = _HEADER
    profile += _FS_RESTRICTED if restrict_fs else _FS_OPEN
    profile += _NETWORK_ALLOW if allow_network else _NETWORK_DENY

    # Build NULL-terminated key/value parameter array
    params = {}
    if restrict_fs:
        params["SANDBOX_ROOT"] = root
    n = len(params)
    arr = (ctypes.c_char_p * (n * 2 + 1))()
    for i, (k, v) in enumerate(params.items()):
        arr[i * 2] = k.encode("utf-8")
        arr[i * 2 + 1] = v.encode("utf-8")
    arr[n * 2] = None

    errbuf = ctypes.c_char_p()

    ret = lib.sandbox_init_with_parameters(
        profile.encode("utf-8"),
        ctypes.c_uint64(0),
        arr,
        ctypes.byref(errbuf),
    )

    if ret != 0:
        msg = (
            errbuf.value.decode("utf-8", errors="replace")
            if errbuf.value
            else "unknown"
        )
        try:
            lib.free(errbuf)
        except Exception:
            pass
        warnings.warn(
            f"Seatbelt profile not applied: {msg}",
            RuntimeWarning,
            stacklevel=2,
        )
        return False

    return True
