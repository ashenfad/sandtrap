"""Safe builtins for sandboxed execution."""

import builtins as _builtins
import contextvars
import copyreg
import functools
import pydoc
import sys
from contextlib import contextmanager
from io import StringIO
from typing import Any, Iterator

# Safe builtin functions (pass-through from real builtins).
SAFE_FN_NAMES = (
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
SAFE_EXCEPTIONS = (
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

for _name in SAFE_FN_NAMES:
    SAFE_BUILTINS[_name] = getattr(_builtins, _name)

for _name in SAFE_EXCEPTIONS:
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


class _FrozenBuiltins(dict):
    """Read-only dict with attribute access for sandbox builtins.

    C-level code (e.g. numpy internals) looks up ``__import__`` via
    ``PyObject_GetAttr(builtins, "__import__")``.  Regular dicts and
    ``MappingProxyType`` do not support arbitrary attribute access.
    This dict subclass adds ``__getattr__`` that falls back to item
    lookup, and blocks all mutation after construction.
    """

    _frozen = False  # class-level default; allows __init__ to populate

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        object.__setattr__(self, "_frozen", True)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setitem__(self, key: str, value: Any) -> None:
        if self._frozen:
            raise TypeError("Cannot modify sandbox builtins")
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        raise TypeError("Cannot modify sandbox builtins")

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("Cannot modify sandbox builtins")

    def __delattr__(self, name: str) -> None:
        raise TypeError("Cannot modify sandbox builtins")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Cannot modify sandbox builtins")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("Cannot modify sandbox builtins")

    def popitem(self) -> tuple[str, Any]:
        raise TypeError("Cannot modify sandbox builtins")

    def clear(self) -> None:
        raise TypeError("Cannot modify sandbox builtins")

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self:
            return self[key]
        raise TypeError("Cannot modify sandbox builtins")

    def __ior__(self, other: Any) -> Any:
        raise TypeError("Cannot modify sandbox builtins")


def _unpickle_real_type(module_name: str, qualname: str) -> type:
    """Resolve a gated type back to its real type on unpickle."""
    import importlib

    mod = importlib.import_module(module_name)
    obj: Any = mod
    for attr in qualname.split("."):
        obj = getattr(obj, attr)
    return obj


class _GatedMeta(type):
    """Metaclass for gated type proxies.

    Each proxy is a real type (passes ``PyType_Check``), so it works
    with ``match/case`` patterns, ``isinstance``, and ``issubclass``.
    Attribute access (e.g. ``int.from_bytes``) delegates to the
    wrapped type.  A custom ``__build_class__`` unwraps proxies when
    used as base classes.
    """

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        real = cls.__gated_real__
        if not cls.__gated_constructable__:
            raise TypeError(f"'{real.__name__}' is not constructable in the sandbox")
        cls.__gated_checkpoint__()
        return real(*args, **kwargs)

    def __instancecheck__(cls, instance: Any) -> bool:
        return isinstance(instance, cls.__gated_real__)

    def __subclasscheck__(cls, subclass: type) -> bool:
        real_sub = getattr(subclass, "__gated_real__", subclass)
        return issubclass(real_sub, cls.__gated_real__)

    def __getattr__(cls, name: str) -> Any:
        return getattr(cls.__gated_real__, name)

    def __repr__(cls) -> str:
        return repr(cls.__gated_real__)


def _make_gated_type(
    real_type: type,
    checkpoint: Any,
    *,
    constructable: bool = True,
) -> type:
    """Create a gated proxy type via ``_GatedMeta``."""
    return _GatedMeta(
        real_type.__name__,
        (),
        {
            "__gated_real__": real_type,
            "__gated_checkpoint__": staticmethod(checkpoint),
            "__gated_constructable__": constructable,
        },
    )


def _reduce_gated(obj: type) -> tuple:
    """Pickle a gated type as its underlying real type."""
    real = obj.__gated_real__
    return _unpickle_real_type, (real.__module__, real.__qualname__)


# Register so pickle uses _reduce_gated instead of save_global for gated types.
copyreg.dispatch_table[_GatedMeta] = _reduce_gated


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

    def _print(
        *args: Any, sep: str = " ", end: str = "\n", file: Any = None, **_kwargs: Any
    ) -> None:
        if file is not None:
            raise ValueError("print(file=...) is not supported in the sandbox")
        text = sep.join(str(a) for a in args) + end
        buffer.write(text)

    return _print


class _SandboxWriter:
    """Write-only stream backing ``sys.stdout`` / ``sys.stderr`` — routes
    to the sandbox's captured output buffer (the same one ``print``
    writes to), so ``sys.stdout.write(...)`` is captured and interleaves
    with prints. Carries the file-like attributes libraries inspect
    (``encoding``/``errors``/``closed``/``isatty``)."""

    encoding = "utf-8"
    errors = "strict"
    closed = False

    def __init__(self, buffer: "StringIO | TailBuffer") -> None:
        self._buffer = buffer

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return len(s)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self._buffer.write(line)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class SandboxSys:
    """A minimal, safe ``sys`` for sandboxed code: only ``stdin``,
    ``stdout``, ``stderr``, ``argv``. It never references the real ``sys``
    module, so no interpreter internals (``modules``, ``settrace``,
    ``_getframe``, ``exit``, ``path``, ...) leak. Attribute access is
    permitted by sandtrap's default policy precisely because these are
    plain public attributes and nothing else exists on the object."""

    def __init__(self, *, stdin: Any, stdout: Any, stderr: Any, argv: list) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.argv = argv


def make_sandbox_sys(
    stdin: Any,
    argv: "list[str] | None",
    stdout_buf: "StringIO | TailBuffer",
    stderr_buf: "StringIO | TailBuffer | None" = None,
) -> SandboxSys:
    """Build the synthetic ``sys`` for one execution. ``stdin`` may be a
    ``str`` (wrapped in a StringIO), a text stream, or ``None`` (empty).
    ``stdout`` routes to the sandbox's captured stdout; ``stderr`` to the
    dedicated stderr capture (falling back to stdout when not given)."""
    if stdin is None:
        stdin_stream: Any = StringIO()
    elif isinstance(stdin, str):
        stdin_stream = StringIO(stdin)
    else:
        stdin_stream = stdin
    writer = _SandboxWriter(stdout_buf)
    return SandboxSys(
        stdin=stdin_stream,
        stdout=writer,
        stderr=_SandboxWriter(stderr_buf) if stderr_buf is not None else writer,
        argv=list(argv) if argv is not None else [""],
    )


def make_input(sandbox_sys: SandboxSys) -> Any:
    """An ``input()`` that reads a line from the sandbox stdin (writing the
    prompt to stdout), matching the real builtin's contract."""

    def _input(prompt: Any = "") -> str:
        if prompt != "":
            sandbox_sys.stdout.write(str(prompt))
        line = sandbox_sys.stdin.readline()
        if line == "":
            raise EOFError("EOF when reading a line")
        return line.rstrip("\r\n")

    return _input


_sandbox_print: contextvars.ContextVar[Any] = contextvars.ContextVar("sandtrap_print")

_original_print = _builtins.print
_print_installed = False


def _patched_print(*args: Any, **kwargs: Any) -> None:
    """Global print replacement that delegates to sandbox print when active."""
    fn = _sandbox_print.get(None)
    if fn is not None:
        fn(*args, **kwargs)
    else:
        _original_print(*args, **kwargs)


def install_print() -> None:
    """Monkeypatch ``builtins.print`` to delegate to the sandbox print
    function when active (via :func:`redirect_print` context manager).

    Outside sandbox execution, ``print()`` behaves normally.
    Idempotent — safe to call multiple times.
    """
    global _print_installed
    if _print_installed:
        return
    _builtins.print = _patched_print
    _print_installed = True


@contextmanager
def redirect_print(print_fn: Any) -> Iterator[None]:
    """Route all print() calls to the sandbox print function."""
    token = _sandbox_print.set(print_fn)
    try:
        yield
    finally:
        _sandbox_print.reset(token)


_sandbox_stdout: contextvars.ContextVar[Any] = contextvars.ContextVar("sandtrap_stdout")
_sandbox_stderr: contextvars.ContextVar[Any] = contextvars.ContextVar("sandtrap_stderr")


class _StreamRouter:
    """``sys.stdout``/``sys.stderr`` replacement that routes writes to
    the active sandbox capture buffer (ContextVar) or falls through to
    the wrapped original stream — the stream counterpart of the
    ``print`` patch.

    Host-library writes during an exec (``pandas``' ``df.info()`` to
    ``sys.stdout``, ``warnings.warn`` to ``sys.stderr``, a registered
    module's direct ``.write``) hit this router on the executing
    context; writes from anywhere else pass through untouched. Unlike a
    ``contextlib.redirect_stdout`` swap, this is safe under concurrent
    executions in one process: each context routes to its own buffer.

    Caveats: code that grabbed a reference to the real stream before
    the router installed (e.g. a ``logging.StreamHandler()`` built at
    import time) bypasses routing — same property as the ``print``
    patch — and C-level writes to the underlying fd never see it.
    """

    _var: contextvars.ContextVar[Any]

    def __init__(self, original: Any) -> None:
        self._original = original

    def write(self, s: str) -> int:
        target = self._var.get(None)
        if target is None:
            target = self._original
        target.write(s)
        return len(s)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        target = self._var.get(None)
        if target is None:
            target = self._original
        flush = getattr(target, "flush", None)
        if flush is not None:
            flush()

    def __getattr__(self, name: str) -> Any:
        # encoding/errors/isatty/fileno/... — delegate to the original
        # stream so libraries inspecting the stream keep working.
        return getattr(self._original, name)


class _StdoutRouter(_StreamRouter):
    _var = _sandbox_stdout


class _StderrRouter(_StreamRouter):
    _var = _sandbox_stderr


def install_stdout() -> None:
    """Monkeypatch ``sys.stdout`` with a router that delegates to the
    active sandbox capture buffer (via :func:`redirect_stdout`) and
    falls through to the original stream otherwise.

    Outside sandbox execution, ``sys.stdout`` behaves normally.
    Idempotent — safe to call multiple times. Checked against the live
    ``sys.stdout`` (not a module flag) so environments that reassign
    it between executions — pytest's capture does, per test — get
    re-wrapped instead of silently losing routing.
    """
    if isinstance(sys.stdout, _StdoutRouter):
        return
    sys.stdout = _StdoutRouter(sys.stdout)


def install_stderr() -> None:
    """``sys.stderr`` counterpart of :func:`install_stdout`."""
    if isinstance(sys.stderr, _StderrRouter):
        return
    sys.stderr = _StderrRouter(sys.stderr)


@contextmanager
def redirect_stdout(buffer: Any) -> Iterator[None]:
    """Route ``sys.stdout`` writes on this context to *buffer*."""
    token = _sandbox_stdout.set(buffer)
    try:
        yield
    finally:
        _sandbox_stdout.reset(token)


@contextmanager
def redirect_stderr(buffer: Any) -> Iterator[None]:
    """Route ``sys.stderr`` writes on this context to *buffer*."""
    token = _sandbox_stderr.set(buffer)
    try:
        yield
    finally:
        _sandbox_stderr.reset(token)


@contextmanager
def passthrough_stdio() -> Iterator[None]:
    """Suspend stdout/stderr/print capture on the current context.

    Host callbacks invoked from inside a sandboxed execution inherit
    the execution's capture routing — their console output lands in
    ``result.stdout``/``result.stderr``. A callback that wants to talk
    to the operator's real console (progress logging, sub-agent
    streaming) wraps its output in this::

        with sandtrap.passthrough_stdio():
            print("visible on the real console")
    """
    tokens = [
        (var, var.set(None))
        for var in (_sandbox_print, _sandbox_stdout, _sandbox_stderr)
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def make_safe_builtins(
    getattr_gate: Any,
    checkpoint: Any = None,
) -> dict[str, Any]:
    """Create a safe builtins dict with policy-gated getattr/hasattr/locals.

    This must be used everywhere sandboxed code executes (main exec,
    VFS modules, reactivated functions/classes) so that ``getattr()``
    and ``hasattr()`` always route through the attribute policy.

    When *checkpoint* is provided, non-type callable builtins get a gate
    that fires one checkpoint (tick + resource check) before executing.
    Type builtins (``str``, ``int``, ``dict``, etc.) are left as real
    types so that library code receiving them (e.g. ``df.astype(str)``,
    ``np.dtype(int)``) works correctly.  Loop-head checkpoints inserted
    by the AST rewriter cover the resource-intensive cases.
    """
    builtins = dict(SAFE_BUILTINS)

    if checkpoint is not None:
        # super() uses frame magic (__class__ cell) — gating breaks it.
        _ungated = frozenset({"super"})

        for name in SAFE_FN_NAMES:
            if name in _ungated:
                continue
            original = builtins[name]
            # Leave real types in builtins so library code that receives
            # them (e.g. pandas .astype(str), numpy dtype(int)) works.
            if isinstance(original, type):
                continue
            elif callable(original):
                fn = original

                @functools.wraps(fn)
                def _gated(*args: Any, _fn: Any = fn, **kwargs: Any) -> Any:
                    checkpoint()
                    return _fn(*args, **kwargs)

                builtins[name] = _gated

        # __build_class__ must unwrap _GatedMeta bases (used for
        # non-constructable registered classes).
        real_build_class = _builtins.__build_class__

        def _gated_build_class(func, name, *bases, **kwargs):
            unwrapped = tuple(
                b.__gated_real__ if isinstance(type(b), _GatedMeta) else b
                for b in bases
            )
            return real_build_class(func, name, *unwrapped, **kwargs)

        builtins["__build_class__"] = _gated_build_class

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
    builtins["dir"] = make_safe_dir()
    # help is injected later by _build_namespace (needs stdout_buf/prints_list)
    return builtins


def _is_internal_name(name: str) -> bool:
    """Return True for sandbox-internal names that should be hidden."""
    return (
        name.startswith("__st_")
        or name == "__builtins__"
        or name == "__name__"
        or name == "print"
    )


def make_safe_locals() -> Any:
    """Create a safe locals() replacement for sandboxed code.

    Returns a function that, when called, produces a filtered copy of
    the caller's local variables — excluding sandbox internals.
    """

    def _safe_locals() -> dict[str, Any]:
        frame = sys._getframe(1)
        return {k: v for k, v in frame.f_locals.items() if not _is_internal_name(k)}

    return _safe_locals


def make_safe_dir() -> Any:
    """Create a safe dir() replacement for sandboxed code.

    With no arguments, returns sorted names from the caller's scope with
    sandbox internals filtered out.  With an argument, delegates to the
    real ``dir(obj)``.
    """
    _real_dir = dir

    def _safe_dir(obj: Any = _SENTINEL) -> list[str]:
        if obj is not _SENTINEL:
            return _real_dir(obj)
        frame = sys._getframe(1)
        return sorted(k for k in frame.f_locals if not _is_internal_name(k))

    return _safe_dir


_SENTINEL = object()


def make_safe_help(
    stdout_buf: StringIO | TailBuffer,
    prints_list: list[tuple[Any, ...]] | None,
) -> Any:
    """Create a safe help() replacement for sandboxed code.

    Renders documentation via ``pydoc`` and writes directly to the
    sandbox's stdout buffer (and prints_list if snapshot_prints is on),
    avoiding ``sys.stdout`` so that sub-agent callbacks and token
    streaming are not intercepted.
    """

    def _safe_help(obj: Any = _SENTINEL) -> None:
        if obj is _SENTINEL:
            text = (
                "Welcome to help!\n\n"
                "To get help on a function or object, call help(thing).\n"
                "For example: help(sorted), help(int), help(my_function)\n"
            )
        else:
            if isinstance(obj, str):
                raise TypeError(
                    "help() with a string argument is not supported in the sandbox"
                )
            text = pydoc.plain(pydoc.render_doc(obj, title="Help on %s"))
        stdout_buf.write(text)
        if prints_list is not None:
            prints_list.append((text,))

    return _safe_help
