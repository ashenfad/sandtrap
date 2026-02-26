"""Unified sandbox factory."""

from __future__ import annotations

from typing import Any, Literal

from .policy import Policy
from .sandbox import Sandbox


def sandbox(
    policy: Policy,
    *,
    isolation: Literal["none", "process", "kernel"] = "none",
    mode: Literal["wrapped", "raw"] = "wrapped",
    filesystem: Any | None = None,
    snapshot_prints: bool = False,
) -> Sandbox:
    """Create a sandbox with the specified isolation level.

    Parameters
    ----------
    policy:
        A :class:`Policy` instance controlling what sandboxed code can access.
    isolation:
        ``"none"`` (default) -- in-process, lightweight.
        ``"process"`` -- fork a worker process (crash protection, no kernel restrictions).
        ``"kernel"`` -- fork a worker + seccomp/Landlock/Seatbelt.
    mode:
        ``"wrapped"`` (default) or ``"raw"``.
    filesystem:
        A ``monkeyfs.FileSystem`` implementation (e.g., ``IsolatedFS``,
        ``VirtualFS``).  When an ``IsolatedFS`` is provided with
        ``isolation="kernel"``, kernel-level filesystem restriction locks
        access to its root directory.  Optional -- when ``None``, sandboxed
        code has no file I/O.
    snapshot_prints:
        When ``True``, deep-copy ``print()`` arguments at call time and
        populate ``result.prints``.  ``result.stdout`` is always captured
        regardless.
    """
    if isolation == "none":
        return Sandbox(
            policy,
            mode=mode,
            filesystem=filesystem,
            snapshot_prints=snapshot_prints,
        )

    # Deferred import — avoid loading multiprocessing for in-process use.
    from .process.sandbox import ProcessSandbox

    kernel_isolation = "auto" if isolation == "kernel" else "none"

    return ProcessSandbox(
        policy,
        filesystem=filesystem,
        mode=mode,
        isolation=kernel_isolation,
        snapshot_prints=snapshot_prints,
    )
