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
    print_handler: Any | None = None,
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
    print_handler:
        Custom print handler.  Only valid with ``isolation="none"``.
    """
    if print_handler is not None and isolation != "none":
        raise ValueError(
            "print_handler is only supported with isolation='none' "
            f"(got isolation={isolation!r})"
        )

    if isolation == "none":
        return Sandbox(
            policy,
            mode=mode,
            filesystem=filesystem,
            print_handler=print_handler,
        )

    # Deferred import — avoid loading multiprocessing for in-process use.
    from .process.sandbox import ProcessSandbox

    kernel_isolation = "auto" if isolation == "kernel" else "none"

    return ProcessSandbox(
        policy,
        filesystem=filesystem,
        mode=mode,
        isolation=kernel_isolation,
    )
