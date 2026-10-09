"""Host time: what an execution's timeout does not count.

The timeout bounds the sandboxed code, not the host it calls. Time inside a
host call -- a registration marked ``host_time=True``, or an RPC to the
parent under process isolation -- moves the start of the clock forward by
exactly as long as the call took. The tick limit still counts the code that
runs, and :meth:`Sandbox.cancel` still stops it, so a host call that never
returns is the embedder's to bound.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class HostClock:
    """One execution's clock, paused while any host call is outstanding.

    Overlapping calls (concurrent tasks, a host call that calls back into
    sandboxed code that makes another) pause it once: the refund is the
    time from the first call in to the last call out, so nothing is
    counted twice.
    """

    def __init__(self, start_box: list[float | None]) -> None:
        self._start = start_box
        self._depth = 0
        self._entered = 0.0
        self._lock = threading.Lock()

    @property
    def paused(self) -> bool:
        return self._depth > 0

    def enter(self) -> None:
        with self._lock:
            if self._depth == 0:
                self._entered = time.monotonic()
            self._depth += 1

    def exit(self) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth == 0 and self._start[0] is not None:
                self._start[0] += time.monotonic() - self._entered

    def remaining(self, timeout: float) -> float | None:
        """Seconds left, or None while paused (the deadline is moving)."""
        if self.paused or self._start[0] is None:
            return None
        return self._start[0] + timeout - time.monotonic()


current_clock: contextvars.ContextVar[HostClock | None] = contextvars.ContextVar(
    "sandtrap_host_clock", default=None
)


@contextmanager
def host_time() -> Iterator[None]:
    """Don't count the time inside this block against the running timeout.

    A no-op outside an execution.
    """
    clock = current_clock.get()
    if clock is None:
        yield
        return
    clock.enter()
    try:
        yield
    finally:
        clock.exit()


def wrap_host_time(fn: Any) -> Any:
    """Wrap a callable so its calls are host time (awaits included)."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with host_time():
                return await fn(*args, **kwargs)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with host_time():
            return fn(*args, **kwargs)

    return wrapper
