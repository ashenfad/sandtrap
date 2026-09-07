"""ProcessSandbox — subprocess-backed sandbox with kernel-level isolation."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import threading
import time
import traceback
import warnings
import weakref
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any, Literal

from ..errors import StForkUnsafe, StPolicyNotPortable
from ..policy import Policy
from ..sandbox import (
    ExecResult,
    IsolationStatus,
    IsolationUnavailable,
    _validate_echo,
    _warn_if_wrapped_mode,
)
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

# Parent-side control connections owned by live Sandtrap workers. A later
# fork inherits these endpoints, so each new child must close its copies or
# it can suppress EOF for an earlier worker after the embedding process exits.
_PARENT_CONNECTIONS: weakref.WeakSet[Any] = weakref.WeakSet()


# Signals a process raises by crashing inside native code. Fork-hostile
# C-library state (allocator thread heaps, Objective-C runtime checks)
# manifests as one of these. A clean nonzero exit means Python-level
# setup failed instead, and SIGKILL means something outside killed it.
_CRASH_SIGNALS = frozenset(
    getattr(signal, name)
    for name in ("SIGSEGV", "SIGBUS", "SIGABRT", "SIGILL", "SIGFPE", "SIGTRAP")
    if hasattr(signal, name)
)


def _exit_signal(process: Any) -> int | None:
    """The signal that killed the worker, or None if it exited normally."""
    if process is None:
        return None
    process.join(timeout=1.0)
    exitcode = process.exitcode
    return -exitcode if exitcode is not None and exitcode < 0 else None


def _describe_exit(process: Any) -> str:
    """Human-readable cause of death for a worker that never became ready."""
    if process is None:
        return "the worker exited before signalling ready"

    process.join(timeout=1.0)
    exitcode = process.exitcode
    if exitcode is None:
        return "the worker stopped responding before signalling ready"
    if exitcode < 0:
        try:
            name = signal.Signals(-exitcode).name
        except ValueError:
            name = f"signal {-exitcode}"
        return f"the worker was killed by {name}"
    return f"the worker exited with status {exitcode}"


def _native_crash_error(process: Any, start_method: str) -> RuntimeError:
    """A worker that crashed in native code without inheriting our memory.

    Same signature as the fork-hostility case, opposite cause: this worker
    started from a fresh interpreter, so none of this process's threads,
    locks, or allocator state reached it. Offering the fork advice here would
    send someone to fix a process they aren't forking.
    """
    return RuntimeError(
        f"Worker process died during initialisation -- {_describe_exit(process)}. "
        f"This worker was created with start_method={start_method!r}, so it "
        "inherited nothing from this process: fork hostility is not the cause, "
        "and constructing the sandbox earlier will not help.\n"
        "\n"
        "A crash this early is the worker's own setup -- most often a granted "
        "module whose import crashes in a fresh interpreter. The worker's "
        "traceback goes to its stderr, which is where the cause will be."
    )


def _init_death_error(process: Any, start_method: str = "fork") -> BaseException:
    """Classify a worker that died before signalling ready.

    Only a crash inside native code indicates fork hostility, and only when
    the worker was actually forked from this process. A clean nonzero exit
    means Python-level setup raised without reporting, and any other signal
    means something external killed the worker -- each deserves its own
    message rather than allocator advice.
    """
    sig = _exit_signal(process)
    if sig is not None and sig in _CRASH_SIGNALS:
        if start_method == "fork":
            return _fork_unsafe_error(process)
        return _native_crash_error(process, start_method)

    detail = ""
    if sig == getattr(signal, "SIGKILL", None):
        detail = (
            " A SIGKILL usually means something outside the process killed it "
            "-- an out-of-memory killer or a supervisor."
        )
    else:
        detail = (
            " The worker's own traceback goes to its stderr, which is where "
            "the cause will be."
        )

    return RuntimeError(
        f"Worker process died during initialisation -- "
        f"{_describe_exit(process)}.{detail}"
    )


def _fork_unsafe_error(process: Any) -> StForkUnsafe:
    """Build the explanatory error for a forked worker that crashed during init.

    Respawning cannot help: the next worker forks the same host process,
    which is still hostile. Only reached under ``start_method="fork"`` now
    that it is opt-in, so the first remedy offered is simply to stop asking
    for it.
    """
    threads = threading.active_count()
    thread_note = (
        f" The host process has {threads} threads running, which is the usual "
        "cause: forking a multi-threaded process leaves C-library state "
        "(allocators, thread registries) broken in the child."
        if threads > 1
        else " The host process appears single-threaded, so suspect "
        "fork-hostile C-extension state rather than threads."
    )

    return StForkUnsafe(
        f"Worker process died during initialisation -- {_describe_exit(process)}."
        f"{thread_note}\n"
        "\n"
        "Respawning will not clear this: each attempt re-forks the same host "
        "process. Fixes, in order of preference:\n"
        '  1. Drop start_method="fork". The default does not fork this '
        "process, so it cannot inherit its threads or C-library state.\n"
        "  2. Construct the sandbox earlier, before the host grows threads.\n"
        "  3. If pyarrow or pandas is loaded, set "
        'os.environ["ARROW_DEFAULT_MEMORY_POOL"] = "system" before the first '
        "import -- its default allocator does not survive fork.\n"
        '  4. Use isolation="none" if a process boundary is not required.\n'
        "\n"
        'See docs/process.md ("How workers are created") for the full list.'
    )


def default_start_method() -> str:
    """How workers are created unless the caller says otherwise.

    ``forkserver`` where it exists: a broker started once from a fresh
    interpreter forks each worker, so no worker inherits the embedding
    process's threads and none of its locks can arrive already held. With the
    preload derived from the policy that costs ~0.7ms per worker against a
    plain fork, which is why this is the default rather than an opt-in.

    ``spawn`` where forkserver is absent, and ``fork`` only if neither exists
    — a position no supported platform is actually in, kept so this can never
    return something unusable. Note that process isolation is POSIX-only for
    reasons unrelated to the start method (cancellation uses ``SIGUSR1``), so
    the non-forkserver branches are defensive rather than exercised.
    """
    available = multiprocessing.get_all_start_methods()
    for candidate in ("forkserver", "spawn", "fork"):
        if candidate in available:
            return candidate
    return "fork"


def _require_portable_policy(policy: Any, start_method: str) -> None:
    """Raise unless *policy* can reach a worker created by *start_method*.

    Reports every problem at once. ``pickle`` surfaces one at a time, from
    inside the serializer, so fixing a policy that way is a loop of rerun,
    read, fix — for something wholly knowable up front.
    """
    check = getattr(policy, "check_picklable", None)
    if check is None:
        return
    problems = check()
    if not problems:
        return

    listed = "\n".join(f"  - {problem}" for problem in problems)
    raise StPolicyNotPortable(
        f"This policy cannot be sent to a start_method={start_method!r} "
        f"worker, which does not inherit this process's memory.\n\n{listed}\n\n"
        'Use start_method="fork" to keep inheriting memory (and its '
        "constraint: forking a multi-threaded host can deadlock the worker), "
        "or adjust the registrations above. See docs/process.md.",
        tuple(problems),
    )


def _forkserver_preload_names(policy: Any, include_grants: bool = False) -> list[str]:
    """Importable module names a forkserver broker should preload.

    Always ``sandtrap`` itself, which we control and which is import-inert.
    That alone takes a worker from ~42ms to ~16ms.

    Granted modules are opt-in, because **preloading runs their import-time
    code in the broker**. A module that starts a background thread on import
    leaves the broker multi-threaded, and every worker forked from it can then
    inherit a lock held by that thread — recreating precisely the permanent
    hang this default exists to prevent. Grants belong to the embedder, so
    only the embedder can say whether that is true of theirs; sandtrap will
    not assume it.

    With grants included a worker costs ~5.3ms against ~4.8ms for a plain
    fork; without, ~16ms. The difference is not worth reintroducing the
    failure silently.

    Names come from each module's real ``__name__`` rather than its
    registration name — they can differ, and only the former is importable.
    """
    names = {"sandtrap"}
    if include_grants:
        for reg in getattr(policy, "modules", {}).values():
            obj = getattr(reg, "obj", None)
            if isinstance(obj, ModuleType):
                name = getattr(obj, "__name__", None)
                if name:
                    names.add(name)
    return sorted(names)


# What the broker THIS process started actually loaded: (owner pid, broker
# pid, module names). Neither of multiprocessing's own signals can answer
# "will my modules be in the broker my worker forks from?":
#
# - ``_preload_modules`` is what was *requested*, and it keeps growing as
#   later sandboxes ask for more. It diverges from what a running broker
#   imported the moment anyone asks for something new.
# - ``_forkserver_pid`` alone can't tell a broker we started from one
#   inherited through fork, and a pre-fork child's inherited pid is stale —
#   its first worker start replaces it with a broker that DOES honour the
#   current list (see ``_reset_inherited_forkserver``).
#
# Both are inherited across fork, so both lie in a forked child. Only a
# record written when a broker of ours starts can answer, and stamping the
# owner pid is what stops a child reading its parent's record as its own.
_BROKER_PRELOAD: "tuple[int, int, frozenset[str]] | None" = None


def _active_broker_preload() -> "frozenset[str] | None":
    """Modules the live broker loaded, or ``None`` when this process has no
    broker it can vouch for — never started one, inherited someone else's, or
    had it replaced since."""
    record = _BROKER_PRELOAD
    if record is None:
        return None
    owner, broker_pid, names = record
    if owner != os.getpid():
        return None  # inherited through fork; the parent's broker, not ours
    from multiprocessing import forkserver as _forkserver_module

    live = getattr(_forkserver_module._forkserver, "_forkserver_pid", None)
    if live != broker_pid:
        return None  # replaced or stopped; we know nothing about the new one
    return names


def _record_broker_preload() -> None:
    """Note what the broker loaded, once it exists. Called after a worker
    starts, which is the first moment the broker's pid is knowable.

    Records the preload list as it stands right after the broker came up —
    for a broker we just started that is exactly what it imported. A broker
    someone else started in this process before our first worker is recorded
    from the same list, which can over-count if they mutated it in between;
    the cost of that is a missed warning, never a false one.
    """
    global _BROKER_PRELOAD
    from multiprocessing import forkserver as _forkserver_module

    server = getattr(_forkserver_module, "_forkserver", None)
    pid = getattr(server, "_forkserver_pid", None)
    if pid is None:
        return
    me = os.getpid()
    if _BROKER_PRELOAD is not None and _BROKER_PRELOAD[:2] == (me, pid):
        return  # already recorded; a running broker's contents never change
    _BROKER_PRELOAD = (me, pid, frozenset(getattr(server, "_preload_modules", ())))


def _apply_forkserver_preload(names: list[str]) -> None:
    """Union *names* into multiprocessing's forkserver preload list.

    The list is process-global and read **once**, when the broker starts, so
    the first worker started in this process fixes the set — a later sandbox
    with different grants re-imports its own modules in the child instead
    (correct, just not free).

    Additive on purpose: never drop an embedder's own preload, and never let
    one sandbox shrink another's.

    Asking for something the running broker doesn't have warns. The entries
    still go on the module-level list, so a broker started later honours them
    — but no worker of the CURRENT one does. Without the warning that reads as
    ``preload_grants=True`` silently doing nothing, which is exactly how it
    presents: the flag is accepted, and worker start stays slow.

    The test is what the broker LOADED (see ``_active_broker_preload``), not
    whether this call grows the requested list. Those differ: the first
    unserved request grows the list, so a second sandbox asking for the same
    module would find it already there and say nothing, while being equally
    unserved. And a process with no broker of its own — including a pre-fork
    child whose inherited pid is about to be replaced by a broker that will
    honour these names — has nothing to warn about.
    """
    from multiprocessing import forkserver as _forkserver_module

    try:
        existing = set(_forkserver_module._forkserver._preload_modules)
    except Exception:
        existing = {"__main__"}  # multiprocessing's own default
    loaded = _active_broker_preload()
    if loaded is not None and (missing := sorted(set(names) - loaded)):
        warnings.warn(
            "the forkserver broker is already running, so it will not preload "
            f"{missing!r} — those modules will be imported in each worker "
            "instead. multiprocessing reads the preload list once, at broker "
            "start, so the first sandbox to start a worker in this process "
            "fixes it. Build sandboxes that need a preload first, or give "
            "them all the same preload_grants value.",
            RuntimeWarning,
            stacklevel=3,
        )
    merged = existing | set(names)
    if merged != existing:
        multiprocessing.set_forkserver_preload(sorted(merged))


def _reset_inherited_forkserver() -> None:
    """Drop forkserver state that belongs to a process we were forked from.

    multiprocessing keeps the broker's pid in a module-level singleton and
    registers no after-fork hook, so a forked child inherits a pid that is not
    its child and ``ensure_running()`` raises ``ChildProcessError``. The
    pre-fork server model — gunicorn, ``uvicorn --workers N`` — hits this
    whenever a broker was started before the supervisor forked.

    Deliberately not ``ForkServer._stop()``: that ``waitpid()``s a process we
    are not the parent of, and ``unlink()``s the *other* process's socket.
    Only our own inherited copies are released here. The lock is replaced
    rather than acquired — if the fork interrupted a broker operation we would
    have inherited it already held, with no thread left to release it.
    """
    global _BROKER_PRELOAD
    from multiprocessing import forkserver as _forkserver_module

    server = getattr(_forkserver_module, "_forkserver", None)
    if server is None:
        return

    # Whatever we knew described the other process's broker. The next worker
    # start raises a broker of our own and re-records it.
    _BROKER_PRELOAD = None

    alive_fd = getattr(server, "_forkserver_alive_fd", None)
    if alive_fd is not None:
        try:
            os.close(alive_fd)  # our copy; the real parent keeps its own
        except OSError:
            pass

    server._forkserver_alive_fd = None
    server._forkserver_pid = None
    server._forkserver_address = None
    server._lock = threading.RLock()


def _is_isolated_fs(filesystem: Any) -> bool:
    try:
        from monkeyfs import IsolatedFS
    except ImportError:
        return False
    return isinstance(filesystem, IsolatedFS)


def _open_file_descriptors() -> tuple[int, ...]:
    """Snapshot descriptors that predate the worker's private plumbing.

    ``fork`` copies descriptors even when they are marked close-on-exec.
    Capture the ambient set *before* creating the worker Pipe/Process so
    the child can discard host capabilities without touching the control
    channel or multiprocessing's bootstrap descriptors.
    """
    from monkeyfs import suspend

    with suspend():
        for fd_dir in ("/proc/self/fd", "/dev/fd"):
            try:
                names = os.listdir(fd_dir)
            except OSError:
                continue

            open_fds: list[int] = []
            for name in names:
                try:
                    fd = int(name)
                except ValueError:
                    continue
                if fd <= 2:
                    continue
                try:
                    os.fstat(fd)
                except OSError:
                    # Directory enumeration may briefly expose the descriptor
                    # used to enumerate the directory itself.
                    continue
                open_fds.append(fd)
            return tuple(open_fds)

    # Supported process-isolation platforms expose one of the descriptor
    # directories above. Fail closed elsewhere: silently returning an empty
    # set would recreate the leak on a new platform.
    raise RuntimeError("Cannot enumerate open file descriptors for forked worker")


def _neutralize_inherited_fds(fds: tuple[int, ...]) -> None:
    """Release inherited host resources without leaving stale fd numbers.

    The fork also copied Python file/socket objects that still remember their
    descriptor numbers. Merely closing the raw descriptors would let those
    stale wrappers later close unrelated descriptors after number reuse.
    Replacing each ambient descriptor with ``/dev/null`` releases the host
    capability while keeping its number safely occupied for the wrapper's
    lifetime.
    """
    if not fds:
        return

    from monkeyfs import suspend

    with suspend():
        devnull = os.open(os.devnull, os.O_RDWR)
        try:
            for fd in fds:
                if fd == devnull:
                    continue
                try:
                    os.dup2(devnull, fd, inheritable=False)
                except OSError:
                    # An at-fork hook may already have closed the descriptor.
                    continue
        finally:
            if devnull not in fds:
                os.close(devnull)


class ProcessSandbox:
    """Subprocess-backed Python sandbox.

    Provides the same ``exec()``/``aexec()``/``cancel()`` interface as
    :class:`~sandtrap.Sandbox`, but runs sandboxed code in an isolated
    child process with optional kernel-level restrictions (seccomp,
    Landlock, Seatbelt).

    The worker process is forked when entering the context manager.  If the
    worker dies (crash, OOM, seccomp kill), the crashing ``exec()`` returns
    an ``ExecResult`` carrying the error and the next ``exec()`` spawns a
    fresh worker transparently — a crash costs the crashing execution (and
    its accumulated worker state), not the sandbox::

        with ProcessSandbox(policy) as sb:
            result = sb.exec("1 + 1")   # OK
            result = sb.exec(crashy)    # result.error: worker died
            result = sb.exec("2 + 2")   # OK again — fresh worker

    **Threading:** The worker is forked by default. Enter the context manager
    before starting threads or async tasks — forking a multithreaded process
    can leave the child holding a lock no surviving thread will release, and
    such a child hangs rather than crashing.

    **File descriptors:** Fork inheritance is preserved by default for
    compatibility with policy registrations that use live resources. Pass
    ``close_fds=True`` to neutralize ambient host descriptors in the child;
    registrations needing live host resources must then bridge them through
    RPC instead. Under a non-fork ``start_method`` the flag is a no-op: the
    child inherits no descriptors to begin with, so what it asks for already
    holds.

    Parameters
    ----------
    policy:
        A :class:`~sandtrap.Policy` instance.  Serialized to the worker
        under the default ``start_method`` (module grants cross by name and
        are re-imported there); inherited through memory under
        ``start_method="fork"``.
    filesystem:
        A ``monkeyfs.FileSystem`` implementation (e.g., ``IsolatedFS``,
        ``VirtualFS``).  When an ``IsolatedFS`` is provided, kernel-level
        filesystem restriction locks access to its root directory.
        Any other filesystem is bridged over the RPC channel (the
        worker sees a ``RemoteFS``), so worker writes land in THIS
        process's instance — an in-memory fs stays the single source
        of truth. Optional — when ``None``, sandboxed code has no
        file I/O.
    mode:
        ``"raw"`` (default) or ``"wrapped"``.  Same as :class:`Sandbox`,
        including that ``"wrapped"`` is deprecated and warns.
    isolation:
        ``"auto"`` applies platform-appropriate kernel sandboxing;
        ``"none"`` skips it.
    start_method:
        How the worker process is created. ``None`` (default) picks the
        safest available — ``"forkserver"`` on POSIX, ``"spawn"`` elsewhere.

        ``"forkserver"`` starts a broker once, from a fresh interpreter, and
        forks each worker from it. Because the broker never grows threads, no
        worker can inherit a lock this process holds — which is what makes a
        forked worker of a multi-threaded host hang.

        The broker preloads ``sandtrap`` itself but **not your grants**, so a
        worker re-imports every granted module. That is what the safety is
        bought with, and it is not free: ~18ms per worker for a stdlib policy,
        but ~235ms and ~113MB resident once a heavyweight stack (pandas, numpy,
        plotly) is granted. ``preload_grants=True`` trades it back — see below.

        ``"fork"`` inherits this process's memory, so the policy needs no
        serialization — the escape hatch for policies that can't cross, at the
        cost of the deadlock hazard above.

        A non-fork worker requires the policy to be serializable (checked at
        construction; see :meth:`Policy.check_picklable`) and the embedding
        program's entry point to be **import-safe** — the child re-imports
        ``__main__``, so module-level work there must sit behind
        ``if __name__ == "__main__":``, and a host started as ``python -c`` or
        from a REPL has no importable ``__main__`` at all. Servers are
        unaffected: an ASGI app is imported, not executed as ``__main__``.
    preload_grants:
        Import the policy's granted modules into the forkserver broker, so
        workers inherit them instead of importing their own copies. Off by
        default. Ignored unless ``start_method="forkserver"``.

        It is a large win where it applies — measured on a
        pandas/numpy/plotly/matplotlib policy, a worker goes from ~235ms and
        ~113MB to ~14ms and ~29MB, since the shared pages are paid for once in
        the broker rather than per worker.

        It is off by default because preloading runs your grants'
        **import-time code in the broker**. A module that starts a background
        thread on import leaves the broker multi-threaded, and a worker forked
        from it can then inherit a lock held by that thread — recreating
        precisely the permanent hang the default start method exists to
        prevent. (The pyarrow allocator note in ``docs/process.md`` applies to
        this configuration too.) Your grants are yours, so only you can say
        whether that is true of them; turn this on when you know they start no
        threads on import.

        **Process-global, first-use-wins.** multiprocessing reads the preload
        list once, when the broker starts, so whichever sandbox starts the
        first worker in this process fixes it. Asking for preload after that
        emits a :class:`RuntimeWarning` and is otherwise a no-op — later
        sandboxes still work, their modules are simply imported per worker.
        Set it on the first sandbox you build, or build them all with the same
        value.
    allow_degraded:
        When ``isolation="auto"`` and the platform can't apply the
        requested kernel mechanisms, ``False`` (default) raises
        :class:`~sandtrap.IsolationUnavailable` when the worker starts;
        ``True`` proceeds with a :class:`RuntimeWarning` and reports the
        shortfall on ``ExecResult.isolation``.  Ignored for
        ``isolation="none"`` (a bare process boundary requests no kernel
        restrictions, so there's nothing to fall short of).
    """

    def __init__(
        self,
        policy: Policy,
        *,
        filesystem: Any | None = None,
        mode: Literal["wrapped", "raw"] = "raw",
        isolation: Literal["auto", "none"] = "auto",
        snapshot_prints: bool = False,
        rpc_handlers: Mapping[str, RpcHandler] | None = None,
        allow_degraded: bool = False,
        echo: Literal["none", "last", "all"] = "none",
        close_fds: bool = False,
        start_method: Literal["fork", "spawn", "forkserver"] | None = None,
        preload_grants: bool = False,
    ) -> None:
        _warn_if_wrapped_mode(mode)
        # None means "whatever is safest here" — forkserver on POSIX. Validate
        # rather than defer: an unavailable method is a construction mistake,
        # and discovering it inside a worker start would report as a worker
        # failure, which is a much worse place to learn it.
        if start_method is None:
            start_method = default_start_method()
        available = multiprocessing.get_all_start_methods()
        if start_method not in available:
            raise ValueError(
                f"start_method={start_method!r} is not available on this "
                f"platform (has: {', '.join(sorted(available))})"
            )
        self._start_method = start_method
        self._preload_grants = preload_grants
        if start_method != "fork":
            # Fail here rather than at worker start. A policy that can't be
            # serialized is a configuration mistake, and this is the only place
            # the embedder can act on it — by first exec it is a worker failure
            # with pickle's first error and no idea which grant caused it.
            _require_portable_policy(policy, start_method)
        self._policy = policy
        self._filesystem = filesystem
        self._mode = mode
        self._isolation = isolation
        self._allow_degraded = allow_degraded
        self._isolation_status: IsolationStatus | None = None
        self._snapshot_prints = snapshot_prints
        # Validate here too — an invalid value would otherwise surface
        # inside the forked worker as a wrapped "Worker failed to
        # initialise" RuntimeError at first exec, instead of a clean
        # ValueError at construction in the host.
        _validate_echo(echo)
        self._echo = echo
        self._close_fds = close_fds
        self._rpc_handlers: dict[str, RpcHandler] = dict(rpc_handlers or {})

        # Bridge non-IsolatedFS filesystems over RPC. Fork inheritance
        # would hand the worker a divergent COPY of an in-memory fs
        # (writes silently lost); RPC keeps the parent's instance the
        # single source of truth. IsolatedFS stays fork-inherited —
        # parent and worker converge on the real directory, and kernel
        # lockdown needs the host root.
        self._worker_fs = filesystem
        if filesystem is not None and not _is_isolated_fs(filesystem):
            from ..fs.remote import FS_RPC_TARGET, RemoteFSMarker, fs_rpc_handler

            self._rpc_handlers.setdefault(FS_RPC_TARGET, fs_rpc_handler(filesystem))
            self._worker_fs = RemoteFSMarker()

        self._process: multiprocessing.Process | None = None
        self._conn: multiprocessing.connection.Connection | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ProcessSandbox:
        self._ensure_worker()
        self._started = True
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

        # Two mechanisms below exist purely to undo fork inheritance, and both
        # are meaningless — descriptor neutralization is actively hazardous —
        # when the child inherits nothing. See docs/forkserver-design.md.
        forking = self._start_method == "fork"

        # Snapshot before creating any worker plumbing. The child neutralizes
        # exactly this ambient set while preserving the Pipe and
        # multiprocessing's own fork-bootstrap descriptors.
        #
        # Fork only: these are the PARENT's descriptor NUMBERS. A spawned child
        # builds its own, so the numbers name unrelated descriptors there —
        # neutralizing them could clobber its own control channel. Nothing is
        # inherited to close, so close_fds is satisfied by construction.
        inherited_fds = _open_file_descriptors() if self._close_fds and forking else ()
        parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
        # Registered whatever the start method: a *later* fork still inherits
        # this endpoint and must close its copy, even if this worker didn't.
        _PARENT_CONNECTIONS.add(parent_conn)
        # Fork only: a spawned child would receive these PICKLED — duplicating
        # live parent endpoints into it rather than cleaning up copies it never
        # got.
        parent_connections = tuple(_PARENT_CONNECTIONS) if forking else ()

        # Fork lets the child inherit this process's memory, so the Policy
        # (with its live module/class references) arrives without pickling.
        # Non-fork methods send it instead — see Policy.__getstate__.
        if self._start_method == "forkserver":
            # Must be set before the broker starts; it is read once, there.
            _apply_forkserver_preload(
                _forkserver_preload_names(self._policy, self._preload_grants)
            )
        ctx = multiprocessing.get_context(self._start_method)
        self._process = ctx.Process(
            target=_worker_entry,
            args=(
                child_conn,
                parent_connections,
                self._policy,
                self._worker_fs,
                self._mode,
                self._isolation,
                self._snapshot_prints,
                self._echo,
                inherited_fds,
            ),
            daemon=True,
        )
        try:
            try:
                self._process.start()
            except ChildProcessError:
                # A broker pid inherited from a process we were forked from —
                # it isn't our child, so multiprocessing's waitpid check fails.
                # Drop the inherited state and let this process start its own.
                # Retried once: a second failure is a real problem, not stale
                # bookkeeping.
                _reset_inherited_forkserver()
                self._process.start()
        except BaseException:
            _PARENT_CONNECTIONS.discard(parent_conn)
            parent_conn.close()
            child_conn.close()
            self._process = None
            raise
        if self._start_method == "forkserver":
            # First moment the broker's pid exists. Note what it loaded, so a
            # later sandbox asking for more can be told it won't get it — the
            # requested list can't answer that (see _apply_forkserver_preload).
            _record_broker_preload()
        child_conn.close()  # Parent doesn't use the child end

        self._conn = parent_conn

        # Wait for ready
        if not self._conn.poll(_READY_TIMEOUT):
            self._kill()
            raise RuntimeError("Worker did not become ready within timeout")

        try:
            msg = self._conn.recv()
        except (EOFError, OSError):
            # Death before ReadyMsg is the fork-hostility signature: the
            # child never got far enough to report a policy problem, so it
            # died in interpreter/C-library setup. Read the exit status
            # before _kill() resets the process handle.
            error = _init_death_error(self._process, self._start_method)
            self._kill()
            raise error

        if isinstance(msg, WorkerErrorMsg):
            self._kill()
            raise RuntimeError(f"Worker failed to initialise:\n{msg.message}")
        if not isinstance(msg, ReadyMsg):
            self._kill()
            raise RuntimeError(f"Unexpected message from worker: {msg!r}")

        # Record what kernel isolation actually took effect, and fail
        # closed if the worker couldn't apply what was asked for. No
        # user code has run yet — the worker is idle at ReadyMsg — so
        # killing here is safe and keeps a degraded worker from ever
        # executing.
        #
        # When kernel isolation was requested (``self._isolation ==
        # "auto"``) a missing status is treated the same as a degraded
        # one: we can't *confirm* the restrictions applied, and
        # fail-closed means unconfirmed is a failure, not a pass. In
        # practice the worker always reports a status; this guards
        # against version skew (an older worker) or a future refactor
        # that drops the field.
        self._isolation_status = msg.isolation
        self._verify_isolation(msg.isolation)

    def _verify_isolation(self, status: IsolationStatus | None) -> None:
        """Accept, warn about, or refuse the isolation the worker reported.

        Entirely parent-side and independent of how the worker was created —
        it reads a status and decides. Separated so that decision can be
        exercised directly: driving it through a real worker means forcing the
        worker to report degraded, which only fork's inherited memory makes
        patchable, and that is a property of the test rather than of the code.
        """
        if self._isolation != "auto" or not (status is None or status.degraded):
            return

        summary = (
            status.summary()
            if status is not None
            else "kernel isolation status missing from worker"
        )
        if not self._allow_degraded:
            self._kill()
            raise IsolationUnavailable(
                f"{summary}. Kernel isolation was requested but "
                "could not be fully applied (or confirmed) on this "
                "platform. Pass allow_degraded=True to proceed with "
                "reduced isolation, or run on a platform with the "
                "required support."
            )
        warnings.warn(
            f"{summary}. Proceeding with reduced isolation (allow_degraded=True).",
            RuntimeWarning,
            stacklevel=4,
        )

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
            _PARENT_CONNECTIONS.discard(self._conn)
            self._conn = None
        self._process = None

    def shutdown(self) -> None:
        """Shut down the worker process cleanly."""
        self._started = False  # exec() raises again until re-entered
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
        stdin: str | Any | None = None,
        argv: list[str] | None = None,
        echo: Literal["none", "last", "all"] | None = None,
    ) -> ExecResult:
        """Execute source code in the sandboxed subprocess.

        ``echo`` overrides the worker sandbox's echo mode for this
        call only (``None`` keeps the construction-time default).

        If a previous execution killed the worker (segfault, OOM,
        seccomp violation), a fresh worker is spawned transparently —
        a crash costs the crashing turn, not the sandbox."""
        if echo is not None:
            _validate_echo(echo)  # fail here, not as worker-error noise
        if not self._started:
            raise RuntimeError(
                "Worker process is not running. "
                "Use ProcessSandbox as a context manager to start it."
            )
        self._ensure_worker()  # respawns after a crash
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
        self._conn.send(
            ExecMsg(source=source, namespace=safe_ns, stdin=stdin, argv=argv, echo=echo)
        )
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
                    stderr=msg.stderr,
                    error=msg.error,
                    ticks=msg.ticks,
                    prints=msg.prints,
                    isolation=self._isolation_status,
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
        stdin: str | Any | None = None,
        argv: list[str] | None = None,
        echo: Literal["none", "last", "all"] | None = None,
    ) -> ExecResult:
        """Execute source code asynchronously in the sandboxed subprocess."""
        if echo is not None:
            _validate_echo(echo)  # fail on the calling task, not in the executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.exec(
                source, namespace=namespace, stdin=stdin, argv=argv, echo=echo
            ),
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
    parent_connections: tuple[multiprocessing.connection.Connection, ...],
    policy: Policy,
    filesystem: Any | None,
    mode: Literal["wrapped", "raw"],
    isolation: Literal["auto", "none"],
    snapshot_prints: bool = False,
    echo: Literal["none", "last", "all"] = "none",
    inherited_fds: tuple[int, ...] = (),
) -> None:
    """Entry point for the worker process (target of multiprocessing.Process)."""
    # Everything before worker_main installs its own reporting must report
    # for itself. Descriptor neutralization and the worker import can both
    # fail (EMFILE, a broken install), and an unreported failure here reaches
    # the parent as a bare EOF -- indistinguishable from a child that died in
    # C-library setup, which would earn it a fork-hostility diagnosis it
    # doesn't deserve.
    try:
        # A fork inherits the parent endpoint for this worker and for every
        # earlier live Sandtrap worker. Close every known copy: otherwise a
        # busy later worker can suppress control EOF and orphan an earlier
        # idle worker when the real parent dies.
        for parent_conn in parent_connections:
            try:
                parent_conn.close()
            except OSError:
                pass
        # Drop the child's copied registry after closing its entries. Only
        # the embedding process owns and tracks parent-side control
        # connections.
        _PARENT_CONNECTIONS.clear()
        _neutralize_inherited_fds(inherited_fds)

        from .worker import worker_main
    except BaseException:
        try:
            conn.send(WorkerErrorMsg(message=traceback.format_exc()))
        except BaseException:
            # The control channel itself is unusable; nothing to report
            # through. The parent will see EOF and describe the exit.
            pass
        return

    worker_main(conn, policy, filesystem, mode, isolation, snapshot_prints, echo)
