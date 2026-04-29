"""Unified sandbox factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
    rpc_handlers: Mapping[str, Callable[[str, tuple, dict], Any]] | None = None,
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
    rpc_handlers:
        Optional mapping of target name to handler callable, only used
        for process / kernel isolation.  When the namespace contains
        an :class:`~sandtrap.RpcProxyMarker` whose ``target`` matches
        a key, the worker substitutes the marker with a proxy whose
        method calls reach the handler in the parent process.  The
        handler receives ``(method, args, kwargs)`` and returns a
        value or raises an exception that propagates to the agent.
        Ignored under in-process isolation (in-process consumers can
        place real objects directly in the namespace).
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
        rpc_handlers=rpc_handlers,
    )
