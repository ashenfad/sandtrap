"""Resource limit helpers for sandboxed execution."""

import resource
import sys


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
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return usage.ru_maxrss  # already bytes
    return usage.ru_maxrss * 1024  # KB → bytes
