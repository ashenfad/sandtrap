"""ProcessSandbox — subprocess-backed sandbox with kernel-level isolation."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time
import warnings
from collections.abc import Callable, Mapping
from typing import Any, Literal

from ..policy import Policy
from ..sandbox import ExecResult
from .protocol import (
    ExecMsg,
    ReadyMsg,
    ResultMsg,
    RpcCallMsg,
    RpcReturnMsg,
    ShutdownMsg,
    WorkerErrorMsg,
    filter_namespace,
)

# Type alias for RPC handler callables registered with the sandbox.
# A handler receives (method_name, args, kwargs) and returns the
# call's result (or raises an exception that gets shipped back to
# the worker and re-raised in the proxy's call site).
RpcHandler = Callable[[str, tuple, dict], Any]

# Timeout (seconds) waiting for worker to become ready
_READY_TIMEOUT = 30.0


class ProcessSandbox:
    """Subprocess-backed Python sandbox.

    Provides the same ``exec()``/``aexec()``/``cancel()`` interface as
    :class:`~sandtrap.Sandbox`, but runs sandboxed code in an isolated
    child process with optional kernel-level restrictions (seccomp,
    Landlock, Seatbelt).

    The worker process is forked when entering the context manager.  If the
    worker dies (crash, OOM, etc.), subsequent ``exec()`` calls raise
    ``RuntimeError`` rather than silently re-forking.  To recover, exit and
    re-enter the context manager::

        with ProcessSandbox(policy) as sb:
            result = sb.exec("1 + 1")  # OK
            # ... worker crashes ...
            result = sb.exec("2 + 2")  # RuntimeError: Worker process is not running.

        # Re-enter to get a fresh worker
        with ProcessSandbox(policy) as sb:
            result = sb.exec("2 + 2")  # OK

    **Threading:** The worker is forked via ``multiprocessing.get_context("fork")``.
    Enter the context manager before starting threads or async tasks to avoid
    forking a multithreaded process, which can deadlock on macOS.

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
        rpc_handlers: Mapping[str, RpcHandler] | None = None,
    ) -> None:
        self._policy = policy
        self._filesystem = filesystem
        self._mode = mode
        self._isolation = isolation
        self._snapshot_prints = snapshot_prints
        self._rpc_handlers: dict[str, RpcHandler] = dict(rpc_handlers or {})

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
        if self._process is None or not self._process.is_alive():
            raise RuntimeError(
                "Worker process is not running. "
                "Use ProcessSandbox as a context manager to start it."
            )
        if self._conn is None:
            raise RuntimeError("No connection to worker process")

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
        return self._await_result()

    def _await_result(self) -> ExecResult:
        """Receive messages from the worker until execution finishes.

        Dispatches RPC calls (``RpcCallMsg``) inline by invoking the
        registered handler and replying with ``RpcReturnMsg``.
        Returns when ``ResultMsg`` arrives or the worker dies /
        becomes unresponsive.

        Unknown message types are warned-and-ignored so future
        protocol additions (e.g. streamed prints) don't break older
        parents.
        """
        # Poll with a deadline so we don't hang forever if the worker
        # becomes unresponsive (e.g., stuck serializing the result).
        # Grace period covers IPC overhead after the sandbox timeout
        # fires.  Reset the deadline whenever we successfully process
        # an RPC call — host-side handler latency shouldn't count
        # against the sandbox timeout.
        assert self._conn is not None  # checked by caller
        deadline = time.monotonic() + self._policy.timeout + 5.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                self._kill()
                return ExecResult(
                    error=RuntimeError("Worker process became unresponsive")
                )
            if not self._process or not self._process.is_alive():
                if self._conn.poll(0.1):
                    pass  # drain final message
                else:
                    self._cleanup()
                    return ExecResult(
                        error=RuntimeError("Worker process died during execution")
                    )
            elif not self._conn.poll(min(1.0, deadline - now)):
                continue

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
            if isinstance(msg, RpcCallMsg):
                # Extend the deadline by the handler's wall-clock
                # duration so host-side time isn't charged to the
                # worker's exec budget — but only by that exact
                # amount.  Resetting the deadline to a fresh
                # ``timeout + grace`` window would let a worker spam
                # RPC calls and dodge the wall-clock limit.
                rpc_start = time.monotonic()
                self._dispatch_rpc(msg)
                deadline += time.monotonic() - rpc_start
                continue

            warnings.warn(
                f"Unknown protocol message {type(msg).__name__!r}; ignoring",
                RuntimeWarning,
                stacklevel=2,
            )

    def _dispatch_rpc(self, msg: RpcCallMsg) -> None:
        """Invoke the named handler and send the result back.

        Errors from the handler are shipped to the worker as
        ``RpcReturnMsg(error=...)`` and re-raised at the worker's
        proxy call site.  Errors that escape *this* method (e.g.
        unpicklable handler return values) become ``RpcReturnMsg``
        carrying a synthesized ``RuntimeError``.
        """
        handler = self._rpc_handlers.get(msg.target)
        if handler is None:
            err: BaseException = RuntimeError(
                f"no rpc handler registered for target {msg.target!r}"
            )
            self._send_rpc_return(msg.call_id, error=err)
            return

        try:
            value = handler(msg.method, msg.args, msg.kwargs)
        except BaseException as exc:
            self._send_rpc_return(msg.call_id, error=exc)
            return

        self._send_rpc_return(msg.call_id, value=value)

    def _send_rpc_return(
        self,
        call_id: str,
        *,
        value: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Send an ``RpcReturnMsg``, sanitizing the payload if the
        original value/error doesn't pickle.

        We try the happy-path send first; if pickling fails we
        substitute a stringified ``RuntimeError`` so the worker at
        least gets a clear (if reduced) signal instead of the
        connection blocking on a half-sent buffer.
        """
        assert self._conn is not None
        try:
            self._conn.send(RpcReturnMsg(call_id=call_id, value=value, error=error))
        except (TypeError, AttributeError, Exception) as send_exc:  # noqa: BLE001
            fallback = RuntimeError(
                f"rpc return for call_id={call_id!r} could not be serialized: "
                f"{send_exc}"
            )
            try:
                self._conn.send(RpcReturnMsg(call_id=call_id, error=fallback))
            except Exception:
                # Connection itself is wedged; let the deadline kick in.
                pass

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
