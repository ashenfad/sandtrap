"""Child process entry point — receives messages and executes sandboxed code."""

from __future__ import annotations

import importlib
import signal
import traceback
import uuid
from multiprocessing.connection import Connection
from typing import Any, Callable, Literal, Mapping

from .protocol import (
    ExecMsg,
    ReadyMsg,
    ResultMsg,
    RpcCallMsg,
    RpcProxyMarker,
    RpcReturnMsg,
    ShutdownMsg,
    WorkerErrorMsg,
    filter_namespace,
    filter_prints,
)


class RpcProxy:
    """Worker-side proxy for a host-side RPC handler.

    Created by substituting :class:`RpcProxyMarker` entries in a
    received namespace before ``sandbox.exec`` runs.  Each method
    call on the proxy sends an :class:`RpcCallMsg` to the parent and
    blocks on the matching :class:`RpcReturnMsg` — the worker is
    single-threaded so one RPC is outstanding at a time.

    Most consumers won't use ``RpcProxy`` directly; the
    :class:`RpcProxyMarker` ``wrapper`` field names a typed class
    (e.g. agex's ``RemoteCache``) that wraps the proxy in a
    domain-specific interface.
    """

    def __init__(self, conn: Connection, target: str) -> None:
        self._conn = conn
        self._target = target

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        call_id = uuid.uuid4().hex
        self._conn.send(
            RpcCallMsg(
                call_id=call_id,
                target=self._target,
                method=method,
                args=args,
                kwargs=kwargs,
            )
        )
        msg = self._conn.recv()
        if not isinstance(msg, RpcReturnMsg):
            raise RuntimeError(
                f"unexpected message type during RPC: {type(msg).__name__}"
            )
        if msg.call_id != call_id:
            raise RuntimeError(
                f"RPC call_id mismatch (sent {call_id!r}, got {msg.call_id!r})"
            )
        if msg.error is not None:
            raise msg.error
        return msg.value

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Dunder lookup falls through to the type, so this only fires
        # for non-dunder attribute access from sandboxed code.
        if name.startswith("_"):
            raise AttributeError(name)

        def bound(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, *args, **kwargs)

        bound.__name__ = name
        return bound

    def __repr__(self) -> str:
        return f"RpcProxy(target={self._target!r})"

    def __reduce__(self):
        """RpcProxy is bound to its worker's Connection — pickling it
        would either fail (kernel mode blocks the resource-sharer
        bind syscall) or succeed misleadingly (the unpickled instance
        wouldn't be tied to anything).  Raising PicklingError lets
        ``filter_namespace`` drop it cleanly when the worker
        sanitises the result namespace before sending it to the
        parent."""
        import pickle

        raise pickle.PicklingError(
            "RpcProxy is bound to its worker connection and can't be pickled "
            "across the process boundary"
        )


def _substitute_proxy_markers(
    namespace: Mapping[str, Any], conn: Connection
) -> dict[str, Any]:
    """Replace top-level ``RpcProxyMarker`` entries with live proxies.

    Walks the namespace once and substitutes each marker with either
    a bare :class:`RpcProxy` or a wrapper class instance (when the
    marker specifies ``wrapper``).  Non-marker values pass through
    unchanged.

    Wrapper imports happen here in the worker, after fork: the
    parent only ships the dotted path string, never an unpicklable
    function reference.
    """
    out: dict[str, Any] = {}
    for k, v in namespace.items():
        if isinstance(v, RpcProxyMarker):
            proxy = RpcProxy(conn, v.target)
            if v.wrapper:
                mod_name, _, cls_name = v.wrapper.partition(":")
                try:
                    mod = importlib.import_module(mod_name)
                    cls = getattr(mod, cls_name)
                    out[k] = cls(proxy, *v.init_args)
                except Exception:
                    # Wrapper resolution failure — surface the bare
                    # proxy so the agent at least gets *something*
                    # callable, with a recognizable class name in
                    # tracebacks.
                    out[k] = proxy
            else:
                out[k] = proxy
        else:
            out[k] = v
    return out


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
                # Replace any RpcProxyMarker placeholders with live
                # proxies bound to this worker's connection before
                # exec runs.  Markers are picklable; live proxies
                # are not (they hold a Connection).
                ns = (
                    _substitute_proxy_markers(msg.namespace, conn)
                    if msg.namespace is not None
                    else None
                )
                result = sandbox.exec(msg.source, namespace=ns)
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
