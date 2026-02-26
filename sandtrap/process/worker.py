"""Child process entry point — receives messages and executes sandboxed code."""

from __future__ import annotations

import signal
import traceback
from multiprocessing.connection import Connection
from typing import Any, Literal

from .protocol import (
    ExecMsg,
    ReadyMsg,
    ResultMsg,
    ShutdownMsg,
    WorkerErrorMsg,
    filter_namespace,
    filter_prints,
)


def worker_main(
    conn: Connection,
    policy: Any,
    filesystem: Any | None,
    mode: Literal["wrapped", "raw"],
    isolation: Literal["auto", "none"],
    snapshot_prints: bool = False,
) -> None:
    """Main loop for the worker subprocess.

    All configuration is passed as arguments (inherited via fork,
    no pickling required).

    Protocol:
    1. Apply isolation, create Sandbox
    2. Send ReadyMsg
    3. Loop: receive ExecMsg/ShutdownMsg, respond accordingly

    Cancellation is handled via SIGUSR1.
    """
    try:
        from monkeyfs.patching import install as install_fs

        from ..sandbox import Sandbox

        # Install monkeyfs patches before building the Sandbox.
        # Sandbox._build_namespace captures builtins.open at call time —
        # if patches aren't installed yet, it captures the original open
        # instead of the interceptor.  Installing early ensures the
        # interceptor is already in place.
        install_fs()

        # Extract root path from IsolatedFS for kernel-level restrictions.
        root: str | None = None
        try:
            from monkeyfs import IsolatedFS

            if isinstance(filesystem, IsolatedFS):
                root = str(filesystem.root)
        except ImportError:
            pass

        # Apply kernel-level isolation before running any user code.
        from .platform import apply_isolation

        apply_isolation(
            isolation,
            root,
            allow_network=policy.needs_network(),
            allow_host_fs=policy.needs_host_fs(),
        )

        sandbox = Sandbox(
            policy, mode=mode, filesystem=filesystem, snapshot_prints=snapshot_prints
        )

        # Install SIGUSR1 handler for cancel — single reader on the pipe,
        # no race conditions.
        def _handle_cancel(signum: int, frame: Any) -> None:
            sandbox.cancel()

        signal.signal(signal.SIGUSR1, _handle_cancel)
    except BaseException:
        conn.send(WorkerErrorMsg(message=traceback.format_exc()))
        return

    conn.send(ReadyMsg())

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break

        if isinstance(msg, ShutdownMsg):
            break

        if isinstance(msg, ExecMsg):
            try:
                result = sandbox.exec(msg.source, namespace=msg.namespace)
                safe_ns = filter_namespace(result.namespace) or {}
                conn.send(
                    ResultMsg(
                        namespace=safe_ns,
                        stdout=result.stdout,
                        error=result.error,
                        ticks=result.ticks,
                        prints=filter_prints(result.prints),
                    )
                )
            except BaseException:
                conn.send(WorkerErrorMsg(message=traceback.format_exc()))
