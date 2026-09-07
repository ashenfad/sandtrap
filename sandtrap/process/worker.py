"""Child process entry point — receives messages and executes sandboxed code."""

from __future__ import annotations

import importlib
import pickle
import signal
import traceback
import uuid
import warnings
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

    def __init__(
        self,
        conn: Connection,
        target: str,
        methods: tuple[str, ...] | None = None,
        attributes: tuple[str, ...] | None = None,
    ) -> None:
        # object.__setattr__: this class refuses attribute writes (see
        # __setattr__), and its own construction must not trip that.
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_methods", methods)
        object.__setattr__(self, "_attributes", attributes)

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

        # A proxy can't inspect the object it stands for, so without a
        # declared surface every name looks like a method and reading a
        # data attribute hands back a function -- which then vanishes
        # from the result namespace as unpicklable, leaving the caller a
        # silent None. Where the surface IS declared, say what's wrong.
        if self._attributes is not None and name in self._attributes:
            raise AttributeError(
                f"{name!r} is a data attribute of the host object, and the "
                "process-isolation bridge carries method calls only. Reading "
                "it here would cross by value and any mutation would be lost, "
                "so it is refused rather than silently copied. Expose a method "
                f"that returns it (e.g. get_{name}()), or run with "
                'isolation="none" where the object itself is in scope.'
            )
        if self._methods is not None and name not in self._methods:
            raise AttributeError(
                f"{name!r} is not part of the host object's exposed surface "
                f"(available: {', '.join(sorted(self._methods)) or 'nothing'})"
            )

        def bound(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, *args, **kwargs)

        bound.__name__ = name
        return bound

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse writes instead of silently landing them on the proxy.

        The host object lives in the parent process; assigning here would
        set an attribute on this stand-in and leave the real object
        untouched, with nothing to indicate the write went nowhere.
        """
        raise AttributeError(
            f"cannot set {name!r}: this is a proxy for a host object in "
            "another process, so the assignment would be lost. Expose a "
            f"method that performs the update (e.g. set_{name}(value))."
        )

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
            proxy = RpcProxy(
                conn,
                v.target,
                methods=getattr(v, "methods", None),
                attributes=getattr(v, "attributes", None),
            )
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


def _warm_deferred_imports() -> None:
    """Import what sandtrap itself defers, before the filesystem is restricted.

    Each entry here is a module reached *lazily* on a path that runs after
    isolation is applied:

    * ``concurrent.futures.thread`` -- ``concurrent.futures`` resolves it in a
      module-level ``__getattr__``, so ``net.patch.install_threading``'s
      reference to ``ThreadPoolExecutor`` triggers the import at exec time.

    Failures are ignored: a module that can't be imported here would not have
    been importable later either, and reporting it as a worker startup failure
    would be worse than letting the real use site raise.
    """
    for name in ("concurrent.futures.thread",):
        try:
            importlib.import_module(name)
        except Exception:
            pass


def worker_main(
    conn: Connection,
    policy: Any,
    filesystem: Any | None,
    mode: Literal["wrapped", "raw"],
    isolation: Literal["auto", "none"],
    snapshot_prints: bool = False,
    echo: Literal["none", "last", "all"] = "none",
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

        # A RemoteFSMarker means the parent kept the real filesystem
        # and registered an RPC handler for it — build the worker-side
        # stub. Every fs operation is a synchronous RPC to the parent,
        # so writes land in the parent's instance (fork inheritance
        # would give this worker a divergent copy).
        from ..fs.remote import FS_RPC_TARGET, RemoteFS, RemoteFSMarker

        if isinstance(filesystem, RemoteFSMarker):
            filesystem = RemoteFS(RpcProxy(conn, FS_RPC_TARGET))

        # Extract root path from IsolatedFS for kernel-level restrictions.
        root: str | None = None
        try:
            from monkeyfs import IsolatedFS

            if isinstance(filesystem, IsolatedFS):
                root = str(filesystem.root)
        except ImportError:
            pass

        # Warm every import the worker still needs, while the filesystem is
        # readable. Landlock confines it to the sandbox root, so a module first
        # touched after apply_isolation cannot be read at all -- and Python's
        # lazy imports make that easy to do by accident. A forked worker
        # inherited these in sys.modules and never noticed; one that starts
        # fresh has to fetch them from disk.
        _warm_deferred_imports()

        # Apply kernel-level isolation before running any user code.
        from .platform import apply_isolation

        isolation_status = apply_isolation(
            isolation,
            root,
            allow_network=policy.needs_network(),
            allow_host_fs=policy.needs_host_fs(),
        )

        with warnings.catch_warnings():
            # The host warned about mode="wrapped" when it built the
            # ProcessSandbox. This construction is the worker's internal
            # copy of that decision, not a second one the embedder made.
            warnings.simplefilter("ignore", DeprecationWarning)
            sandbox = Sandbox(
                policy,
                mode=mode,
                filesystem=filesystem,
                snapshot_prints=snapshot_prints,
                echo=echo,
            )

        # Install SIGUSR1 handler for cancel — single reader on the pipe,
        # no race conditions.
        def _handle_cancel(signum: int, frame: Any) -> None:
            sandbox.cancel()

        signal.signal(signal.SIGUSR1, _handle_cancel)
    except BaseException:
        conn.send(WorkerErrorMsg(message=traceback.format_exc()))
        return

    conn.send(ReadyMsg(isolation=isolation_status))

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
                result = sandbox.exec(
                    msg.source,
                    namespace=ns,
                    stdin=msg.stdin,
                    argv=msg.argv,
                    echo=msg.echo,
                )
                safe_ns = filter_namespace(result.namespace) or {}
                err = result.error
                if err is not None and err.__traceback__ is not None:
                    # Traceback objects don't survive pickling, so the
                    # parent would see a bare message. Render the full
                    # traceback HERE, where the frames still exist, and
                    # ride it across in the exception's __dict__ (which
                    # BaseException.__reduce__ preserves).
                    try:
                        err._st_traceback_text = "".join(
                            traceback.format_exception(err)
                        ).rstrip()
                    except Exception:
                        pass  # __slots__/frozen exceptions: message-only
                if err is not None:
                    # An exception that can't pickle (unpicklable
                    # attributes riding on it) would kill the send and
                    # replace the agent's error with worker-crash
                    # noise. Ship a stand-in that tells the same story
                    # — same class name, message, and rendered frames.
                    try:
                        pickle.dumps(err)
                    except Exception:
                        try:
                            message = f"{type(err).__name__}: {err}"
                        except Exception:
                            message = f"unrepresentable {type(err).__name__}"
                        fallback = RuntimeError(message)
                        tb_text = getattr(err, "_st_traceback_text", None)
                        if isinstance(tb_text, str):
                            fallback._st_traceback_text = tb_text
                        err = fallback
                conn.send(
                    ResultMsg(
                        namespace=safe_ns,
                        stdout=result.stdout,
                        error=err,
                        ticks=result.ticks,
                        prints=filter_prints(result.prints),
                        stderr=result.stderr,
                    )
                )
            except BaseException:
                conn.send(WorkerErrorMsg(message=traceback.format_exc()))
