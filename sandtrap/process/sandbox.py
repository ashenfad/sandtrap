"""ProcessSandbox — subprocess-backed sandbox with kernel-level isolation."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time
import warnings
from collections.abc import Mapping
from typing import Any, Literal

from ..policy import Policy
from ..sandbox import ExecResult
from .protocol import (
    ExecMsg,
    ReadyMsg,
    ResultMsg,
    ShutdownMsg,
    WorkerErrorMsg,
    filter_namespace,
)

# Timeout (seconds) waiting for worker to become ready
_READY_TIMEOUT = 30.0


class ProcessSandbox:
    """Subprocess-backed Python sandbox.

    Provides the same ``exec()``/``aexec()``/``cancel()`` interface as
    :class:`~sandtrap.Sandbox`, but runs sandboxed code in an isolated
    child process with optional kernel-level restrictions (seccomp,
    Landlock, Seatbelt).

    Parameters
    ----------
    policy:
        A :class:`~sandtrap.Policy` instance.  Inherited by the child
        process via fork.
    filesystem:
        A ``monkeyfs.FileSystem`` implementation (e.g., ``IsolatedFS``,
        ``VirtualFS``).  When an ``IsolatedFS`` is provided, kernel-level
        filesystem restriction locks access to its root directory.
        Optional — when ``None``, sandboxed code has no file I/O.
    mode:
        ``"wrapped"`` (default) or ``"raw"``.  Same as :class:`Sandbox`.
    isolation:
        ``"auto"`` applies platform-appropriate kernel sandboxing;
        ``"none"`` skips it.
    """

    def __init__(
        self,
        policy: Policy,
        *,
        filesystem: Any | None = None,
        mode: Literal["wrapped", "raw"] = "wrapped",
        isolation: Literal["auto", "none"] = "auto",
        snapshot_prints: bool = False,
    ) -> None:
        self._policy = policy
        self._filesystem = filesystem
        self._mode = mode
        self._isolation = isolation
        self._snapshot_prints = snapshot_prints

        self._process: multiprocessing.Process | None = None
        self._conn: multiprocessing.connection.Connection | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ProcessSandbox:
        self._ensure_worker()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Spawn the worker process if not already running."""
        if self._process is not None and self._process.is_alive():
            return

        # Clean up dead worker if needed
        if self._process is not None:
            self._cleanup()

        parent_conn, child_conn = multiprocessing.Pipe(duplex=True)

        # Use fork context — the child inherits the parent's memory
        # space, so the Policy (with its live module/class references)
        # is available directly without pickling.
        ctx = multiprocessing.get_context("fork")
        self._process = ctx.Process(
            target=_worker_entry,
            args=(
                child_conn,
                self._policy,
                self._filesystem,
                self._mode,
                self._isolation,
                self._snapshot_prints,
            ),
            daemon=True,
        )
        self._process.start()
        child_conn.close()  # Parent doesn't use the child end

        self._conn = parent_conn

        # Wait for ready
        if not self._conn.poll(_READY_TIMEOUT):
            self._kill()
            raise RuntimeError("Worker did not become ready within timeout")

        try:
            msg = self._conn.recv()
        except (EOFError, OSError):
            self._kill()
            raise RuntimeError("Worker process died during initialisation")

        if isinstance(msg, WorkerErrorMsg):
            self._kill()
            raise RuntimeError(f"Worker failed to initialise:\n{msg.message}")
        if not isinstance(msg, ReadyMsg):
            self._kill()
            raise RuntimeError(f"Unexpected message from worker: {msg!r}")

    def _kill(self) -> None:
        """Force-kill the worker process."""
        if self._process is not None and self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=2.0)
        self._cleanup()

    def _cleanup(self) -> None:
        """Close connection and reset state."""
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        self._process = None

    def shutdown(self) -> None:
        """Shut down the worker process cleanly."""
        if self._conn is not None:
            try:
                self._conn.send(ShutdownMsg())
            except (OSError, BrokenPipeError):
                pass
        if self._process is not None:
            self._process.join(timeout=5.0)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=2.0)
        self._cleanup()

    # ------------------------------------------------------------------
    # Reactivation
    # ------------------------------------------------------------------

    def _reactivate_namespace(self, result: ExecResult) -> ExecResult:
        """Reactivate St* wrappers that crossed the process boundary.

        Namespace values and error payloads (e.g. TaskSuccess.result)
        containing StFunction/StClass/StInstance arrive inactive after
        deserialization.  This rebuilds gates from the policy and
        reactivates them so they're callable on the parent side.
        """
        from ..gates import make_gates
        from ..wrappers import StClass, StFunction, StInstance, activate_value

        st_types = (StFunction, StClass, StInstance)

        def _find_st_objects(value):
            """Yield St* objects from a value, walking one level into containers."""
            if isinstance(value, st_types):
                yield value
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, st_types):
                        yield item
            elif isinstance(value, dict):
                for v in value.values():
                    if isinstance(v, st_types):
                        yield v

        # Collect all St* objects from namespace and error payload
        sources = list(result.namespace.values())
        if hasattr(result.error, "result"):
            sources.append(result.error.result)

        found = [obj for src in sources for obj in _find_st_objects(src)]
        if not found:
            return result

        gates = make_gates(self._policy)
        ns = result.namespace
        for obj in found:
            activate_value(obj, gates, namespace=ns)

        return result

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def exec(
        self,
        source: str,
        *,
        namespace: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        """Execute source code in the sandboxed subprocess."""
        self._ensure_worker()
        assert self._conn is not None

        safe_ns = filter_namespace(namespace)
        if namespace is not None and safe_ns is not None:
            for k in namespace:
                if k not in safe_ns:
                    warnings.warn(
                        f"Namespace key {k!r} skipped: value is not picklable",
                        RuntimeWarning,
                        stacklevel=2,
                    )
        self._conn.send(ExecMsg(source=source, namespace=safe_ns))

        # Poll with a deadline so we don't hang forever if the worker
        # becomes unresponsive (e.g., stuck serializing the result).
        # Grace period covers IPC overhead after the sandbox timeout fires.
        deadline = time.monotonic() + self._policy.timeout + 5.0
        while time.monotonic() < deadline:
            if not self._process or not self._process.is_alive():
                # Child died — drain any remaining message
                if self._conn.poll(0.1):
                    break
                self._cleanup()
                return ExecResult(
                    error=RuntimeError("Worker process died during execution")
                )
            if self._conn.poll(1.0):
                break
        else:
            self._kill()
            return ExecResult(error=RuntimeError("Worker process became unresponsive"))

        try:
            msg = self._conn.recv()
        except (EOFError, OSError):
            self._cleanup()
            return ExecResult(
                error=RuntimeError("Worker process died during execution")
            )

        if isinstance(msg, ResultMsg):
            result = ExecResult(
                namespace=msg.namespace,
                stdout=msg.stdout,
                error=msg.error,
                ticks=msg.ticks,
                prints=msg.prints,
            )
            return self._reactivate_namespace(result)
        if isinstance(msg, WorkerErrorMsg):
            return ExecResult(error=RuntimeError(f"Worker error:\n{msg.message}"))

        return ExecResult(error=RuntimeError(f"Unexpected message: {msg!r}"))

    async def aexec(
        self,
        source: str,
        *,
        namespace: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        """Execute source code asynchronously in the sandboxed subprocess."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.exec(source, namespace=namespace)
        )

    def cancel(self) -> None:
        """Cancel the currently running execution.

        Safe to call from any thread.  Sends SIGUSR1 to the worker
        process, which triggers ``Sandbox.cancel()`` in the child.
        """
        if self._process is not None and self._process.is_alive():
            try:
                os.kill(self._process.pid, signal.SIGUSR1)
            except (OSError, ProcessLookupError):
                pass


def _worker_entry(
    conn: multiprocessing.connection.Connection,
    policy: Policy,
    filesystem: Any | None,
    mode: Literal["wrapped", "raw"],
    isolation: Literal["auto", "none"],
    snapshot_prints: bool = False,
) -> None:
    """Entry point for the worker process (target of multiprocessing.Process)."""
    from .worker import worker_main

    worker_main(conn, policy, filesystem, mode, isolation, snapshot_prints)
