"""Resource limit helpers for sandboxed execution."""

import sys
import warnings
from contextlib import contextmanager
from typing import Iterator

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]


def get_rss_bytes() -> int:
    """Return the peak RSS of this process in bytes.

    Uses ``getrusage(RUSAGE_SELF).ru_maxrss`` which reports the peak
    (high-water-mark) resident set size, not the current RSS.  On macOS
    the value is in bytes; on Linux it is in kilobytes and is converted
    here.

    Because this is the peak, the memory limit check is conservative:
    once peak RSS has grown by more than the limit during a sandbox
    execution, subsequent checkpoints will continue to trip even if
    memory has since been freed.
    """
    if resource is None:
        raise RuntimeError("Memory limits require the 'resource' module (Unix only)")
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return usage.ru_maxrss  # already bytes
    return usage.ru_maxrss * 1024  # KB → bytes


@contextmanager
def memory_limit_context(mb: int) -> Iterator[None]:
    """Set RLIMIT_AS for the duration of the context.

    Computes current peak RSS and sets the virtual address space limit
    to ``current + mb * 1024 * 1024``.  The kernel refuses allocations
    beyond this limit, raising ``MemoryError``.

    This is **process-wide** — concurrent sandboxes share the limit.

    Platform notes:
    - **Linux**: kernel-enforced, works reliably.
    - **macOS**: ``RLIMIT_AS`` is not supported (``setrlimit`` returns
      ``EINVAL``).  Falls back to checkpoint-based memory detection.
    - **Windows**: no ``resource`` module — no-op with a warning.
    """
    if resource is None:
        warnings.warn(
            "memory_limit requires the 'resource' module (Unix only)",
            RuntimeWarning,
            stacklevel=2,
        )
        yield
        return

    baseline = get_rss_bytes()
    headroom = mb * 1024 * 1024
    new_soft = baseline + headroom

    try:
        old_soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    except (ValueError, OSError):
        warnings.warn(
            "Could not read RLIMIT_AS; memory limit not enforced",
            RuntimeWarning,
            stacklevel=2,
        )
        yield
        return

    try:
        resource.setrlimit(resource.RLIMIT_AS, (new_soft, hard))
    except (ValueError, OSError):
        # macOS does not support RLIMIT_AS (returns EINVAL).
        # Checkpoint-based memory detection still works as fallback.
        yield
        return

    try:
        yield
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (old_soft, hard))
        except (ValueError, OSError):
            pass  # may fail if already over old limit
