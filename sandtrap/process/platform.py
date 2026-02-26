"""Platform detection and isolation dispatch."""

from __future__ import annotations

import sys
from typing import Literal


def apply_isolation(
    mode: Literal["auto", "none"],
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> None:
    """Apply platform-appropriate kernel-level isolation.

    Called once in the child process after fork, before any user code.

    Parameters
    ----------
    mode:
        ``"auto"`` to apply available restrictions, ``"none"`` to skip.
    root:
        Absolute path to the sandbox filesystem root directory, or None
        if no real-path filesystem restriction is needed.
    allow_network:
        If True, allow network syscalls through the kernel filter.
    allow_host_fs:
        If True, skip kernel-level filesystem restriction (the policy
        has registrations that need host filesystem access).
    """
    if mode == "none":
        return

    if sys.platform == "linux":
        _apply_linux(root, allow_network=allow_network, allow_host_fs=allow_host_fs)
    elif sys.platform == "darwin":
        _apply_darwin(root, allow_network=allow_network, allow_host_fs=allow_host_fs)
    # Other platforms: no kernel-level isolation available


def _apply_linux(
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> None:
    """Apply Landlock + seccomp on Linux."""
    from . import landlock, seccomp

    # Landlock first (filesystem restriction), then seccomp (syscall filter).
    # Order matters: Landlock setup requires syscalls that seccomp may block.
    if root is not None and not allow_host_fs:
        landlock.apply(root)
    seccomp.apply(allow_network=allow_network)


def _apply_darwin(
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> None:
    """Apply Seatbelt on macOS."""
    from . import seatbelt

    seatbelt.apply(root, allow_network=allow_network, allow_host_fs=allow_host_fs)
