"""sandtrap error types and traceback utilities."""

import os
import types

_SBLITE_DIR = os.path.dirname(os.path.abspath(__file__))


class StError(Exception):
    """Base exception for all sandtrap sandbox errors."""

    pass


class StTimeout(StError):
    """Raised when sandbox execution exceeds the configured timeout."""

    pass


class StCancelled(StError):
    """Raised when sandbox execution is cancelled externally."""

    pass


class StTickLimit(StError):
    """Raised when sandbox execution exceeds the configured tick limit."""

    pass


class StValidationError(StError):
    """Raised when AST validation rejects code before compilation."""

    def __init__(self, message: str, lineno: int | None = None, col: int | None = None):
        self.lineno = lineno
        self.col = col
        super().__init__(message)


def strip_internal_frames(exc: BaseException) -> BaseException:
    """Strip leading sandtrap-internal frames from an exception's traceback.

    Sets ``exc.__traceback__`` to the first frame that belongs to user
    sandbox code or external code.  Frames that appear *after* the first
    user frame are left intact (Python does not expose an API to relink
    ``tb_next``).
    """
    tb = exc.__traceback__
    if tb is None:
        return exc

    first_user = _find_first_user_frame(tb)
    if first_user is not None:
        exc.__traceback__ = first_user

    return exc


def _is_internal_frame(filename: str) -> bool:
    """Check if a filename belongs to sandtrap internals."""
    if filename.startswith("<sandtrap:"):
        return False  # User sandbox code
    try:
        return os.path.abspath(filename).startswith(_SBLITE_DIR)
    except (ValueError, OSError):
        return False


def _find_first_user_frame(
    tb: types.TracebackType,
) -> types.TracebackType | None:
    """Find the first traceback frame that's not sandtrap internal code."""
    current: types.TracebackType | None = tb
    while current is not None:
        if not _is_internal_frame(current.tb_frame.f_code.co_filename):
            return current
        current = current.tb_next
    return tb  # Fallback: return original if all frames are internal
