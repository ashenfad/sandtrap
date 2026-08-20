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


class StForkUnsafe(StError, RuntimeError):
    """Raised when a process worker dies before signalling ready.

    Workers are forked from the embedding process, so this almost always
    means the host is no longer fork-safe -- it has grown threads or
    fork-hostile C-library state since startup. Respawning re-forks the
    same hostile process, so the condition does not clear on its own.

    Subclasses ``RuntimeError`` as well as ``StError``: this path used to
    raise a bare ``RuntimeError``, and callers already catching that
    should keep working.

    Only raised under ``start_method="fork"``. The default creates workers
    that do not inherit this process at all, so they cannot reach this state;
    see ``docs/process.md`` ("How workers are created").
    """

    pass


class StPolicyNotPortable(StError, ValueError):
    """A policy can't reach a worker that isn't forked from this process.

    Raised at construction, not at worker start, and listing *every* problem
    rather than the first: a policy that can't be serialized is a
    configuration mistake, and the embedder can only act on it where they
    wrote it. See ``Policy.check_picklable()``.

    Subclasses ``ValueError`` as well as ``StError`` so ordinary
    ``except ValueError`` handlers around construction keep working.
    """

    def __init__(self, message: str, problems: tuple = ()):
        self.problems = problems
        super().__init__(message)


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
