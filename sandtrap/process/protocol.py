"""Message types for parent↔child pipe communication.

Configuration (policy, root, mode, isolation) is passed as Process args
via fork inheritance — no pickling required.  Only per-execution
messages go through the pipe.

The protocol is a tagged-message exchange.  Top-level types are:

- Parent → Worker: ``ExecMsg`` (request execution),
  ``ShutdownMsg`` (terminate).
- Worker → Parent: ``ReadyMsg`` (initialised), ``ResultMsg``
  (execution complete), ``WorkerErrorMsg`` (worker-level failure).

While exec is running, the worker may also send ``RpcCallMsg`` to
request that the parent invoke a host-side handler.  The parent
replies with ``RpcReturnMsg``.  The parent's exec dispatch loop
recognises ``RpcCallMsg`` and routes it; consumers add new tagged
message types in the same shape — unknown tags are warned-and-
ignored, so adding messages is forward-compatible.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..sandbox import IsolationStatus


def filter_prints(
    prints: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    """Drop non-picklable print snapshots.

    Each entry is a tuple of args from a single ``print()`` call.
    Entries that fail to pickle are silently dropped.
    """
    safe: list[tuple[Any, ...]] = []
    for entry in prints:
        try:
            pickle.dumps(entry)
            safe.append(entry)
        except (pickle.PicklingError, TypeError, AttributeError):
            pass
    return safe


def filter_namespace(ns: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop non-picklable values from a namespace dict.

    Returns a new dict containing only pickle-safe key/value pairs.
    Used by both the parent (to sanitise outgoing namespaces) and the
    worker (to sanitise result namespaces before sending them back).
    """
    if ns is None:
        return None
    filtered: dict[str, Any] = {}
    for k, v in ns.items():
        try:
            pickle.dumps(v)
            filtered[k] = v
        except Exception:
            # Anything: pickling arbitrary objects raises arbitrary
            # exceptions (a closed StringIO raises ValueError;
            # __reduce__ hooks raise whatever they like). Unpicklable
            # means dropped, never fatal.
            pass
    return filtered


# ---------------------------------------------------------------------------
# Parent → Child
# ---------------------------------------------------------------------------


@dataclass
class ExecMsg:
    """Request execution of sandboxed code."""

    source: str
    namespace: Mapping[str, Any] | None
    stdin: Any | None = None
    argv: list[str] | None = None
    echo: str | None = None  # per-exec override; None = sandbox default


@dataclass
class ShutdownMsg:
    """Cleanly terminate the worker."""


# ---------------------------------------------------------------------------
# Child → Parent
# ---------------------------------------------------------------------------


@dataclass
class ReadyMsg:
    """Worker is initialised and ready for exec requests.

    Carries the :class:`~sandtrap.IsolationStatus` recorded when the
    worker applied kernel isolation after fork, so the parent can decide
    whether a degraded result is acceptable before running user code.
    """

    isolation: IsolationStatus | None = None


@dataclass
class ResultMsg:
    """Execution completed (success or sandbox-level error)."""

    namespace: dict[str, Any]
    stdout: str
    error: BaseException | None
    ticks: int
    prints: list[tuple[Any, ...]]
    stderr: str = ""


@dataclass
class WorkerErrorMsg:
    """Worker-level failure (not a sandbox error)."""

    message: str


# ---------------------------------------------------------------------------
# Mid-exec: worker ↔ parent RPC
# ---------------------------------------------------------------------------


@dataclass
class RpcCallMsg:
    """Worker → parent: invoke a host-side handler synchronously.

    The worker substitutes ``RpcProxyMarker`` entries in the namespace
    with proxies that send these messages on each method call and
    block on the matching :class:`RpcReturnMsg`.

    ``call_id`` is included for diagnostic correlation; the worker is
    single-threaded so only one RPC is outstanding at a time.
    """

    call_id: str
    target: str
    method: str
    args: tuple
    kwargs: dict


@dataclass
class RpcReturnMsg:
    """Parent → worker: result of a previous :class:`RpcCallMsg`.

    Exactly one of ``value`` / ``error`` should be set.  When
    ``error`` is set, the worker re-raises it in the proxy's call
    site so the agent sees the original exception.
    """

    call_id: str
    value: Any = None
    error: BaseException | None = None


def rpc_surface(
    obj: Any, policy: Any = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a host object's public surface into ``(methods, attributes)``.

    Built parent-side, where the object lives, and shipped on an
    :class:`RpcProxyMarker` so the worker's proxy can tell a method from a
    data attribute without asking. Pass the ``policy`` to narrow the result
    to what it actually permits — the proxy then refuses everything else by
    name, which is the only place those filters reach a bridged object.

    Underscore-prefixed names are excluded: the proxy refuses them anyway.
    Attributes that raise on access (properties with side effects) are
    skipped rather than allowed to break marker construction.
    """
    methods: list[str] = []
    attributes: list[str] = []
    for name in dir(obj):
        if name.startswith("_"):
            continue
        if policy is not None and not policy.is_attr_allowed(obj, name):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        (methods if callable(value) else attributes).append(name)
    return tuple(methods), tuple(attributes)


@dataclass
class RpcProxyMarker:
    """Picklable placeholder injected into the namespace by the parent.

    The worker substitutes each marker with an RPC proxy bound to its
    connection before calling ``sandbox.exec``.  ``target`` matches a
    key in the parent's ``rpc_handlers`` dict — that's the host-side
    handler the proxy's calls reach.

    ``wrapper`` is an optional ``"module:Class"`` dotted path; when
    set, the worker imports and applies it to wrap the raw proxy in
    a typed object (e.g. agex's ``RemoteCache`` wraps the proxy in a
    ``MutableMapping`` interface).  When ``None``, the agent gets the
    bare ``RpcProxy`` instance.

    ``methods`` and ``attributes`` declare the object's surface so the
    worker's proxy can answer for it locally. Without them the proxy
    cannot tell a method from a data attribute — it returns a caller
    for *every* name, so reading ``obj.token`` silently yields a
    function instead of a value. Supplying them turns that into a
    clear error. ``None`` (the default) keeps the older permissive
    behaviour for embedders that haven't declared a surface yet; see
    :func:`sandtrap.rpc_surface` for building them from a live object.
    """

    target: str
    wrapper: str | None = None
    init_args: tuple = field(default_factory=tuple)
    methods: tuple[str, ...] | None = None
    """Names the proxy may call. ``None`` means "undeclared, allow any"."""
    attributes: tuple[str, ...] | None = None
    """Data attribute names — declared so the proxy can say *why* they
    don't work, rather than handing back a callable that isn't one."""
