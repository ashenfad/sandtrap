"""Main sandbox entry point."""

import ast
import itertools
import linecache
import threading
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from ._traceback import strip_internal_frames
from .builtins import SAFE_BUILTINS, TailBuffer, make_print, make_safe_locals
from .fs.protocol import FileSystem
from .gates import _wrap_privileged, make_gates
from .policy import Policy
from .rewriter import Rewriter

_exec_counter = itertools.count(1)
_INTERNAL_KEYS = {"__builtins__", "__name__"}


class _NonConstructable:
    """Proxy for a class registered with constructable=False.

    Supports isinstance/issubclass checks but raises TypeError on call.
    """

    def __init__(self, cls: type) -> None:
        self._cls = cls

    def __instancecheck__(self, instance: Any) -> bool:
        return isinstance(instance, self._cls)

    def __subclasscheck__(self, subclass: type) -> bool:
        return issubclass(subclass, self._cls)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(
            f"'{self._cls.__name__}' is not constructable in the sandbox"
        )

    def __repr__(self) -> str:
        return f"<non-constructable '{self._cls.__name__}'>"


@dataclass
class ExecResult:
    """Result of a sandbox execution."""

    namespace: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    error: BaseException | None = None


class Sandbox:
    """Lightweight in-process Python sandbox.

    Parses, validates, rewrites, compiles, and executes Python code
    under a policy-controlled security model.
    """

    def __init__(
        self,
        policy: Policy,
        *,
        mode: Literal["task", "service"] = "task",
        filesystem: FileSystem | None = None,
    ) -> None:
        self.policy = policy
        self.mode = mode
        self.filesystem = filesystem
        self._cancel_flag = threading.Event()

        # Install patches eagerly so _build_namespace captures patched versions
        if filesystem is not None:
            from .fs.patch import install as install_fs

            install_fs()
        if not policy.allow_network:
            from .net.patch import install as install_net

            install_net()


    def cancel(self) -> None:
        """Cancel the currently running execution.

        Safe to call from any thread.  The sandbox will raise
        ``SbCancelled`` at the next checkpoint (loop iteration or
        function entry).
        """
        self._cancel_flag.set()

    def _build_namespace(
        self,
        namespace: Mapping[str, Any] | None,
        gates: dict[str, Any],
        stdout_buf: TailBuffer,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the execution namespace with builtins, gates, and registered items.

        Returns (namespace, injected) where injected maps name → value for
        items that should be filtered from result.namespace (unless reassigned).
        """
        ns: dict[str, Any] = dict(namespace) if namespace else {}
        injected: dict[str, Any] = {}

        ns["__builtins__"] = dict(SAFE_BUILTINS)
        ns.setdefault("__name__", "__sblite__")
        ns.update(gates)

        # Gate-aware getattr/hasattr that enforce the attr whitelist
        _sb_getattr = gates["__sb_getattr__"]

        def _safe_getattr(obj: Any, name: str, *default: Any) -> Any:
            try:
                return _sb_getattr(obj, name)
            except AttributeError:
                if default:
                    return default[0]
                raise

        def _safe_hasattr(obj: Any, name: str) -> bool:
            try:
                _sb_getattr(obj, name)
                return True
            except AttributeError:
                return False

        ns["__builtins__"]["getattr"] = _safe_getattr
        ns["__builtins__"]["hasattr"] = _safe_hasattr
        ns["__builtins__"]["locals"] = make_safe_locals()

        # Populate registered functions (with privilege wrapping)
        for fn_name, fn_reg in self.policy.functions.items():
            fn = fn_reg.func
            if fn_reg.network_access or fn_reg.host_fs_access:
                fn = _wrap_privileged(
                    fn,
                    network_access=fn_reg.network_access,
                    host_fs_access=fn_reg.host_fs_access,
                )
            ns.setdefault(fn_name, fn)
            injected[fn_name] = ns[fn_name]

        # Populate registered classes
        for cls_name, cls_reg in self.policy.classes.items():
            if cls_reg.constructable:
                ns.setdefault(cls_name, cls_reg.cls)
            else:
                ns.setdefault(cls_name, _NonConstructable(cls_reg.cls))
            injected[cls_name] = ns[cls_name]

        # Provide open() if filesystem interception is active
        if self.filesystem is not None:
            import builtins as _builtins

            ns["__builtins__"]["open"] = _builtins.open

        # Capture stdout
        print_fn = make_print(stdout_buf)
        ns["print"] = print_fn
        injected["print"] = print_fn

        return ns, injected

    def _memory_params(self) -> tuple[int | None, int | None]:
        """Return (memory_limit_bytes, start_rss) for checkpoint enforcement."""
        if self.policy.memory_limit is None:
            return None, None
        from .resource_limits import get_rss_bytes

        return self.policy.memory_limit * 1024 * 1024, get_rss_bytes()

    def _make_stdout_buf(self) -> TailBuffer:
        """Create a stdout buffer, respecting max_stdout policy."""
        return TailBuffer(max_chars=self.policy.max_stdout)

    def _enter_sandbox_context(self, stack: ExitStack) -> None:
        """Set up network denial and filesystem interception on the ExitStack."""
        if not self.policy.allow_network:
            from .net.context import deny_network
            from .net.patch import install as install_net

            install_net()
            stack.enter_context(deny_network())

        if self.filesystem is not None:
            from .fs.context import use_fs
            from .fs.patch import install as install_fs

            install_fs()
            stack.enter_context(use_fs(self.filesystem))

    def exec(
        self,
        source: str,
        *,
        namespace: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        """Execute source code synchronously in the sandbox."""
        # 1. Parse
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return ExecResult(error=e)

        # 2. Rewrite (validate + transform)
        task_mode = self.mode == "task"
        rewriter = Rewriter(task_mode=task_mode)
        tree = rewriter.visit(tree)

        # 3. Fix missing locations
        ast.fix_missing_locations(tree)

        # 4. Register source in linecache for tracebacks
        filename = f"<sblite:{next(_exec_counter)}>"
        lines = source.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        linecache.cache[filename] = (
            len(source),
            None,
            lines,
            filename,
        )

        # 5. Compile
        code = compile(tree, filename, "exec")

        # 6. Build namespace
        import time

        self._cancel_flag.clear()
        mem_limit_bytes, start_rss = self._memory_params()
        gates = make_gates(
            self.policy,
            _start_time=time.monotonic(),
            _cancel_flag=self._cancel_flag,
            _func_asts=rewriter._func_asts if task_mode else None,
            _class_asts=rewriter._class_asts if task_mode else None,
            _task_mode=task_mode,
            _memory_limit_bytes=mem_limit_bytes,
            _start_rss=start_rss,
            _filesystem=self.filesystem,
        )
        stdout_buf = self._make_stdout_buf()
        ns, injected = self._build_namespace(namespace, gates, stdout_buf)

        # 7. Execute with sandbox context
        error = None
        with ExitStack() as stack:
            self._enter_sandbox_context(stack)
            try:
                exec(code, ns)  # noqa: S102
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise  # Real Ctrl-C from host
                error = strip_internal_frames(e)

        # 8. Build clean result namespace (keep original ns intact for globals)
        result_ns = {
            k: v
            for k, v in ns.items()
            if k not in _INTERNAL_KEYS
            and not k.startswith("__sb_")
            and not (k in injected and v is injected[k])
        }

        # 9. Clean up linecache entry
        linecache.cache.pop(filename, None)

        return ExecResult(
            namespace=result_ns,
            stdout=stdout_buf.getvalue(),
            error=error,
        )

    def activate(
        self,
        obj: Any,
        *,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        """Activate an unpickled SbFunction/SbClass/SbInstance."""
        from .wrappers import SbClass, SbFunction, SbInstance

        gates = make_gates(self.policy)
        if isinstance(obj, SbFunction):
            obj.activate(gates, namespace=namespace)
        elif isinstance(obj, SbClass):
            obj.activate(gates, namespace=namespace)
        elif isinstance(obj, SbInstance):
            sb_class = object.__getattribute__(obj, "_sb_class")
            if sb_class._compiled_cls is None:
                sb_class.activate(gates, namespace=namespace)
            obj.activate()
        else:
            raise TypeError(f"Cannot activate {type(obj).__name__}")

    async def aexec(
        self,
        source: str,
        *,
        namespace: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        """Execute source code asynchronously in the sandbox."""
        import asyncio
        import time

        # 1. Parse
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return ExecResult(error=e)

        # 2. Rewrite (validate + transform)
        task_mode = self.mode == "task"
        rewriter = Rewriter(task_mode=task_mode)
        tree = rewriter.visit(tree)

        # 3. Wrap body in: async def __sb_aexec__(): ...; return __sb_locals__()
        return_locals = ast.Return(
            value=ast.Call(
                func=ast.Name(id="__sb_locals__", ctx=ast.Load()),
                args=[],
                keywords=[],
            )
        )
        wrapper_kwargs: dict[str, Any] = dict(
            name="__sb_aexec__",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=tree.body + [return_locals],
            decorator_list=[],
            returns=None,
        )
        # type_params added in Python 3.12
        if hasattr(ast.AsyncFunctionDef, "type_params"):
            wrapper_kwargs["type_params"] = []
        wrapper = ast.AsyncFunctionDef(**wrapper_kwargs)
        tree.body = [wrapper]

        # 4. Fix missing locations
        ast.fix_missing_locations(tree)

        # 5. Register source in linecache
        filename = f"<sblite:{next(_exec_counter)}>"
        lines = source.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        linecache.cache[filename] = (
            len(source),
            None,
            lines,
            filename,
        )

        # 6. Compile
        code = compile(tree, filename, "exec")

        # 7. Build namespace
        self._cancel_flag.clear()
        mem_limit_bytes, start_rss = self._memory_params()
        gates = make_gates(
            self.policy,
            _start_time=time.monotonic(),
            _cancel_flag=self._cancel_flag,
            _func_asts=rewriter._func_asts if task_mode else None,
            _class_asts=rewriter._class_asts if task_mode else None,
            _task_mode=task_mode,
            _memory_limit_bytes=mem_limit_bytes,
            _start_rss=start_rss,
            _filesystem=self.filesystem,
        )
        stdout_buf = self._make_stdout_buf()
        ns, injected = self._build_namespace(namespace, gates, stdout_buf)

        # Inject locals() under an internal name for the async wrapper
        import builtins as _builtins
        ns["__sb_locals__"] = _builtins.locals

        # 8. Execute to define __sb_aexec__, then await it
        error = None
        result_locals: dict[str, Any] = {}
        with ExitStack() as stack:
            self._enter_sandbox_context(stack)
            try:
                exec(code, ns)  # noqa: S102
                coro = ns["__sb_aexec__"]()
                timeout = self.policy.timeout
                result_locals = await asyncio.wait_for(coro, timeout=timeout)
                if result_locals is None:
                    result_locals = {}
            except asyncio.TimeoutError:
                from .errors import SbTimeout

                error = SbTimeout(
                    f"Execution exceeded {self.policy.timeout}s timeout"
                )
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise  # Real Ctrl-C from host
                error = strip_internal_frames(e)

        # 9. Build result namespace from locals + globals
        result_ns: dict[str, Any] = {}
        # Include globals set by user code
        for k, v in ns.items():
            if (
                k not in _INTERNAL_KEYS
                and not k.startswith("__sb_")
                and not (k in injected and v is injected[k])
            ):
                result_ns[k] = v
        # Overlay with locals from the async wrapper
        for k, v in result_locals.items():
            if not k.startswith("__sb_"):
                result_ns[k] = v

        # 10. Clean up linecache entry
        linecache.cache.pop(filename, None)

        return ExecResult(
            namespace=result_ns,
            stdout=stdout_buf.getvalue(),
            error=error,
        )
