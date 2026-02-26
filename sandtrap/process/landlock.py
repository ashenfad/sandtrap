"""Landlock filesystem restriction (Linux 5.13+).

Restricts the current process's filesystem access to a single directory.
Requires the ``landlock`` PyPI package.  Graceful no-op if the package
is missing or the kernel does not support Landlock.
"""

from __future__ import annotations

import sys
import warnings


def available() -> bool:
    """Return True if Landlock can be applied on this system."""
    if sys.platform != "linux":
        return False
    try:
        from landlock import landlock_abi_version

        landlock_abi_version()
        return True
    except (ImportError, OSError):
        return False


def apply(root: str) -> bool:
    """Restrict filesystem access to *root* via Landlock.

    Returns True if restrictions were applied, False if Landlock is
    unavailable (missing package, unsupported kernel, etc.).
    """
    if sys.platform != "linux":
        return False

    try:
        from landlock import Ruleset
    except ImportError:
        warnings.warn(
            "landlock package not installed; Landlock restrictions not applied",
            RuntimeWarning,
            stacklevel=2,
        )
        return False

    try:
        # Deny all filesystem access except within root
        rs = Ruleset()
        rs.allow(root)
        rs.apply()
        return True
    except OSError as exc:
        warnings.warn(
            f"Landlock not applied: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return False
