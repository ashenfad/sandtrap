"""Message types for parent↔child pipe communication.

Configuration (policy, root, mode, isolation) is passed as Process args
via fork inheritance — no pickling required.  Only per-execution
messages go through the pipe.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
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
