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
        except (pickle.PicklingError, TypeError, AttributeError):
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


@dataclass
class ShutdownMsg:
    """Cleanly terminate the worker."""


# ---------------------------------------------------------------------------
# Child → Parent
# ---------------------------------------------------------------------------


@dataclass
class ReadyMsg:
    """Worker is initialised and ready for exec requests."""


@dataclass
class ResultMsg:
    """Execution completed (success or sandbox-level error)."""

    namespace: dict[str, Any]
    stdout: str
    error: BaseException | None
    ticks: int
    prints: list[tuple[Any, ...]]


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
    """

    target: str
    wrapper: str | None = None
    init_args: tuple = field(default_factory=tuple)
