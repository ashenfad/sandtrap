"""Platform detection and isolation dispatch."""

from __future__ import annotations

import sys
from typing import Literal

from ..sandbox import IsolationStatus


def apply_isolation(
    mode: Literal["auto", "none"],
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> IsolationStatus:
    """Apply platform-appropriate kernel-level isolation.

    Called once in the child process after fork, before any user code.
    Applies restrictions best-effort and reports exactly what took
    effect in the returned :class:`IsolationStatus` — it never raises or
    warns on unavailability.  The *decision* about whether a degraded
    result is acceptable belongs to the parent (see
    ``ProcessSandbox``), which holds the caller's ``allow_degraded``
    choice; the worker's job is only to apply and report.

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
    status = IsolationStatus(requested=(mode == "auto"), platform=sys.platform)
    if mode == "none":
        return status

    if sys.platform == "linux":
        _apply_linux(
            status, root, allow_network=allow_network, allow_host_fs=allow_host_fs
        )
    elif sys.platform == "darwin":
        _apply_darwin(
            status, root, allow_network=allow_network, allow_host_fs=allow_host_fs
        )
    # Other platforms: no kernel-level isolation available — status
    # keeps its default None flags, so ``degraded`` reports True.
    return status


def _apply_linux(
    status: IsolationStatus,
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> None:
    """Apply Landlock + seccomp on Linux, recording what took effect."""
    from . import landlock, seccomp

    # Landlock first (filesystem restriction), then seccomp (syscall filter).
    # Order matters: Landlock setup requires syscalls that seccomp may block.
    if root is not None and not allow_host_fs:
        status.landlock = landlock.apply(root)
    status.seccomp = seccomp.apply(allow_network=allow_network)


def _apply_darwin(
    status: IsolationStatus,
    root: str | None,
    *,
    allow_network: bool = False,
    allow_host_fs: bool = False,
) -> None:
    """Apply Seatbelt on macOS, recording what took effect."""
    from . import seatbelt

    status.seatbelt = seatbelt.apply(
        root, allow_network=allow_network, allow_host_fs=allow_host_fs
    )
