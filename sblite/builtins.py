"""Safe builtins for sandboxed execution."""

import builtins as _builtins
from io import StringIO
from typing import Any

# Safe builtin functions (pass-through from real builtins).
_SAFE_FN_NAMES = (
    "abs",
    "all",
    "any",
    "ascii",
    "bin",
    "bool",
    "bytearray",
    "bytes",
    "callable",
    "chr",
    "complex",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "hash",
    "hex",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "oct",
    "ord",
    "pow",
    "property",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "staticmethod",
    "classmethod",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "zip",
)

# Safe exception types available in sandbox.
_SAFE_EXCEPTIONS = (
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BlockingIOError",
    "BrokenPipeError",
    "BufferError",
    "BytesWarning",
    "ChildProcessError",
    "ConnectionAbortedError",
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "DeprecationWarning",
    "EOFError",
    "Exception",
    "FileExistsError",
    "FileNotFoundError",
    "FloatingPointError",
    "FutureWarning",
    "IOError",
    "ImportError",
    "IndentationError",
    "IndexError",
    "InterruptedError",
    "IsADirectoryError",
    "KeyError",
    "LookupError",
    "MemoryError",
    "ModuleNotFoundError",
    "NameError",
    "NotADirectoryError",
    "NotImplementedError",
    "OSError",
    "OverflowError",
    "PendingDeprecationWarning",
    "PermissionError",
    "ProcessLookupError",
    "RecursionError",
    "ReferenceError",
    "ResourceWarning",
    "RuntimeError",
    "RuntimeWarning",
    "StopAsyncIteration",
    "StopIteration",
    "SyntaxError",
    "SyntaxWarning",
    "SystemError",
    "TabError",
    "TimeoutError",
    "TypeError",
    "UnboundLocalError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "UnicodeError",
    "UnicodeTranslateError",
    "UnicodeWarning",
    "UserWarning",
    "ValueError",
    "Warning",
    "ZeroDivisionError",
)

SAFE_BUILTINS: dict[str, Any] = {}

for _name in _SAFE_FN_NAMES:
    SAFE_BUILTINS[_name] = getattr(_builtins, _name)

for _name in _SAFE_EXCEPTIONS:
    _val = getattr(_builtins, _name, None)
    if _val is not None:
        SAFE_BUILTINS[_name] = _val


# Restrict type() to single-argument (inspection) form only.
# The three-argument form type('X', bases, dict) creates classes
# and would bypass the AST rewriter's class-definition validation.
def _safe_type(obj, /):
    return type(obj)


SAFE_BUILTINS["type"] = _safe_type

# Constants
SAFE_BUILTINS["True"] = True
SAFE_BUILTINS["False"] = False
SAFE_BUILTINS["None"] = None
SAFE_BUILTINS["Ellipsis"] = Ellipsis
SAFE_BUILTINS["NotImplemented"] = NotImplemented
SAFE_BUILTINS["__build_class__"] = _builtins.__build_class__


class TailBuffer:
    """A write-only string buffer that keeps only the most recent bytes.

    When the total written content exceeds *max_chars*, earlier output is
    discarded and a truncation marker is prepended on read.
    """

    _TRUNCATION_MARKER = "... [earlier output truncated]\n"

    def __init__(self, max_chars: int | None = None) -> None:
        self._buf = StringIO()
        self._max = max_chars
        self._truncated = False

    def write(self, text: str) -> None:
        self._buf.write(text)
        if self._max is not None and self._buf.tell() > self._max:
            # Keep the tail
            content = self._buf.getvalue()
            keep = content[-self._max :]
            self._buf = StringIO()
            self._buf.write(keep)
            self._truncated = True

    def getvalue(self) -> str:
        content = self._buf.getvalue()
        if self._truncated:
            return self._TRUNCATION_MARKER + content
        return content


def make_print(buffer: StringIO | TailBuffer) -> Any:
    """Create a print function that writes to the given buffer."""

    def _print(*args: Any, sep: str = " ", end: str = "\n", file: Any = None, **_kwargs: Any) -> None:
        if file is not None:
            raise ValueError("print(file=...) is not supported in the sandbox")
        text = sep.join(str(a) for a in args) + end
        buffer.write(text)

    return _print


def make_safe_builtins(getattr_gate: Any) -> dict[str, Any]:
    """Create a safe builtins dict with policy-gated getattr/hasattr/locals.

    This must be used everywhere sandboxed code executes (main exec,
    VFS modules, reactivated functions/classes) so that ``getattr()``
    and ``hasattr()`` always route through the attribute policy.
    """
    builtins = dict(SAFE_BUILTINS)

    def _safe_getattr(obj: Any, name: str, *default: Any) -> Any:
        try:
            return getattr_gate(obj, name)
        except AttributeError:
            if default:
                return default[0]
            raise

    def _safe_hasattr(obj: Any, name: str) -> bool:
        try:
            getattr_gate(obj, name)
            return True
        except AttributeError:
            return False

    builtins["getattr"] = _safe_getattr
    builtins["hasattr"] = _safe_hasattr
    builtins["locals"] = make_safe_locals()
    return builtins


def make_safe_locals() -> Any:
    """Create a safe locals() replacement for sandboxed code.

    Returns a function that, when called, produces a filtered copy of
    the caller's local variables — excluding sandbox internals.
    """
    import sys

    def _safe_locals() -> dict[str, Any]:
        frame = sys._getframe(1)
        return {
            k: v
            for k, v in frame.f_locals.items()
            if not k.startswith("__sb_")
            and k != "__builtins__"
            and k != "__name__"
            and k != "print"
        }

    return _safe_locals
