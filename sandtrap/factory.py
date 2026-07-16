"""Unified sandbox factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from .policy import Policy
from .sandbox import Sandbox

if TYPE_CHECKING:
    from .process.sandbox import ProcessSandbox


def sandbox(
    policy: Policy,
    *,
    isolation: Literal["none", "process", "kernel"] = "none",
    mode: Literal["wrapped", "raw"] = "wrapped",
    filesystem: Any | None = None,
    snapshot_prints: bool = False,
    rpc_handlers: Mapping[str, Callable[[str, tuple, dict], Any]] | None = None,
    allow_degraded: bool = False,
    echo: Literal["none", "last", "all"] = "none",
) -> Sandbox | ProcessSandbox:
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
    allow_degraded:
        Only meaningful for ``isolation="kernel"``.  When the platform
        can't apply the requested kernel mechanisms (missing package,
        kernel too old, unsupported OS), ``False`` (default) raises
        :class:`~sandtrap.IsolationUnavailable` rather than silently
        running user code with reduced isolation.  Set ``True`` to
        proceed anyway with a :class:`RuntimeWarning`; the shortfall is
        reported on ``ExecResult.isolation``.  Ignored for ``"none"``
        and ``"process"`` (neither requests kernel restrictions).
    echo:
        REPL/notebook-style auto-display of top-level expression
        statements (``"none"`` default, off).  ``"all"`` echoes every
        bare top-level expression; ``"last"`` echoes only a final
        expression statement (Jupyter's ``last_expr``).  An echoed
        value lands in both output channels at its execution position:
        its ``repr`` in ``result.stdout`` and, with
        ``snapshot_prints=True``, the raw object in ``result.prints``
        as a single-arg entry (an implicit ``print``).  ``None`` values
        are suppressed, so ``print(x)`` never double-echoes.
    """
    if isolation == "none":
        return Sandbox(
            policy,
            mode=mode,
            filesystem=filesystem,
            snapshot_prints=snapshot_prints,
            echo=echo,
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
        allow_degraded=allow_degraded,
        echo=echo,
    )
