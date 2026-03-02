"""Main sandbox entry point."""

import ast
import asyncio
import builtins as _builtins
import copy
import itertools
import linecache
import threading
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from .builtins import (
    TailBuffer,
    _FrozenBuiltins,
    _make_gated_type,
    make_print,
    make_safe_builtins,
    make_safe_help,
)
from .errors import StTimeout, StValidationError, strip_internal_frames
from .fs import FileSystem, patch
from .gates import make_gates, wrap_privileged
from .net.context import deny_network
from .net.patch import install as install_net
from .policy import Policy
from .resource_limits import get_rss_bytes, memory_limit_context
from .rewriter import Rewriter
from .wrappers import ModuleRef, StClass, StFunction, StInstance, activate_value

_exec_counter = itertools.count(1)
_INTERNAL_KEYS = {"__builtins__", "__name__"}


@dataclass
class ExecResult:
    """Result of a sandbox execution."""

    namespace: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    error: BaseException | None = None
    ticks: int = 0
    prints: list[tuple[Any, ...]] = field(default_factory=list)


class Sandbox:
    """Lightweight in-process Python sandbox.

    Parses, validates, rewrites, compiles, and executes Python code
    under a policy-controlled security model.
    """

    def __init__(
        self,
        policy: Policy,
        *,
        mode: Literal["wrapped", "raw"] = "wrapped",
        filesystem: FileSystem | None = None,
        snapshot_prints: bool = False,
    ) -> None:
        self.policy = policy
        self.mode = mode
        self.filesystem = filesystem
        self.snapshot_prints = snapshot_prints
        self._cancel_flag = threading.Event()

        # Install FS-aware patches once (idempotent, permanent) so that
        # builtins.open is the patched version *before* _build_namespace
        # captures it into the sandbox namespace.
        if filesystem is not None:
            from monkeyfs.patching import install as install_fs

            install_fs()

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._cancel_flag.clear()

    def cancel(self) -> None:
        """Cancel the currently running execution.

        Safe to call from any thread.  The sandbox will raise
        ``StCancelled`` at the next checkpoint (loop iteration or
        function entry).
        """
        self._cancel_flag.set()

    def _auto_activate(
        self,
        ns: dict[str, Any],
        gates: dict[str, Any],
    ) -> None:
        """Auto-activate any inactive StFunction/StClass/StInstance in namespace."""
        import_gate = gates.get("__st_import__")
        for k, v in list(ns.items()):
            activate_value(v, gates, sandbox=self, namespace=ns)
            if isinstance(v, ModuleRef) and import_gate is not None:
                try:
                    top = v.name.split(".")[0]
                    if k == top:
                        # Bare dotted import (import pkg.mod) — return top-level package
                        ns[k] = import_gate(v.name)
                    else:
                        # Aliased import (import pkg.mod as m) — return leaf module
                        ns[k] = import_gate(v.name, alias=k)
                except Exception:
                    pass  # VFS file may no longer exist

    def _attach_sandbox_refs(
        self,
        ns: dict[str, Any],
        gates: dict[str, Any],
    ) -> None:
        """Attach sandbox/gates refs to wrappers that don't have them yet.

        Functions and classes created during exec() need these refs so
        that direct calls from host code get full sandbox protections.
        """
        for v in ns.values():
            if isinstance(v, StFunction) and v._sandbox is None:
                v._sandbox = self
                v._gates = gates
            elif isinstance(v, StClass) and v._sandbox is None:
                v._sandbox = self
                v._gates = gates

    def _call_in_context(
        self,
        fn: Any,
        gates: dict[str, Any],
        args: tuple,
        kwargs: dict[str, Any],
    ) -> Any:
        """Call a compiled function with full sandbox context.

        Resets checkpoint state (ticks, timeout, memory) and enters
        the sandbox context (fs/net patches) for the duration of the call.
        """
        # Reset checkpoint state for this call
        gates["__st_tick_counter__"][0] = 0
        gates["__st_start_time__"][0] = time.monotonic()
        self._cancel_flag.clear()
        gates["__st_cancel_flag__"][0] = self._cancel_flag
        mem_limit, start_rss = self._memory_params()
        gates["__st_memory__"][0] = mem_limit
        gates["__st_memory__"][1] = start_rss

        with ExitStack() as stack:
            self._enter_sandbox_context(stack)
            return fn(*args, **kwargs)

    def _build_namespace(
        self,
        namespace: Mapping[str, Any] | None,
        gates: dict[str, Any],
        stdout_buf: TailBuffer,
        prints_list: list[tuple[Any, ...]] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the execution namespace with builtins, gates, and registered items.

        Returns (namespace, injected) where injected maps name → value for
        items that should be filtered from result.namespace (unless reassigned).
        """
        ns: dict[str, Any] = dict(namespace) if namespace else {}
        injected: dict[str, Any] = {}

        ns["__builtins__"] = make_safe_builtins(
            gates["__st_getattr__"],
            checkpoint=gates["__st_checkpoint__"],
        )
        ns.setdefault("__name__", "__sandtrap__")
        ns.update(gates)

        # Populate registered functions (with privilege wrapping)
        for fn_name, fn_reg in self.policy.functions.items():
            fn = fn_reg.func
            if fn_reg.network_access or fn_reg.host_fs_access:
                fn = wrap_privileged(
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
                ns.setdefault(
                    cls_name,
                    _make_gated_type(
                        cls_reg.cls,
                        gates["__st_checkpoint__"],
                        constructable=False,
                    ),
                )
            injected[cls_name] = ns[cls_name]

        # Populate registered modules (available directly by name, not just via import)
        for mod_name, mod_reg in self.policy.modules.items():
            ns.setdefault(mod_name, mod_reg.obj)
            injected[mod_name] = ns[mod_name]

        # Provide open() if filesystem interception is active
        if self.filesystem is not None:
            ns["__builtins__"]["open"] = _builtins.open

        # Build print function: checkpoint + stdout capture + optional snapshot
        checkpoint = gates["__st_checkpoint__"]
        stdout_handler = make_print(stdout_buf)

        def print_fn(*args: Any, **kwargs: Any) -> None:
            checkpoint()
            if prints_list is not None:
                try:
                    snapped = copy.deepcopy(args)
                except Exception:
                    snapped = args
                prints_list.append(snapped)
            stdout_handler(*args, **kwargs)

        ns["print"] = print_fn
        injected["print"] = print_fn

        # help() writes directly to stdout_buf/prints_list instead of
        # sys.stdout, so sub-agent callbacks are not intercepted.
        help_fn = make_safe_help(stdout_buf, prints_list)
        ns["help"] = help_fn
        injected["help"] = help_fn

        # Provide the real __import__ so C extensions (e.g. numpy, pandas)
        # can import their transitive dependencies.  User-code imports are
        # gated at the AST level — the rewriter validates every import
        # statement against the policy.  Direct access to __import__ via
        # __builtins__ is blocked by the AST rewriter (it treats
        # __builtins__ as a blocked name).
        ns["__builtins__"]["__import__"] = _builtins.__import__

        # Freeze builtins so sandboxed code cannot mutate them.
        # _FrozenBuiltins adds __getattr__ so C-level PyObject_GetAttr
        # lookups (like __import__) work alongside normal item access.
        ns["__builtins__"] = _FrozenBuiltins(ns["__builtins__"])

        return ns, injected

    def _memory_params(self) -> tuple[int | None, int | None]:
        """Return (memory_limit_bytes, start_rss) for checkpoint enforcement."""
        if self.policy.memory_limit is None:
            return None, None
        return self.policy.memory_limit * 1024 * 1024, get_rss_bytes()

    def _make_stdout_buf(self) -> TailBuffer:
        """Create a stdout buffer, respecting max_stdout policy."""
        return TailBuffer(max_chars=self.policy.max_stdout)

    def _enter_sandbox_context(self, stack: ExitStack) -> None:
        """Set up memory limits, network denial, and filesystem interception."""
        if self.policy.memory_limit is not None:
            stack.enter_context(memory_limit_context(self.policy.memory_limit))

        if not self.policy.allow_network:
            install_net()
            stack.enter_context(deny_network())

        if self.filesystem is not None:
            stack.enter_context(patch(self.filesystem))

    # ------------------------------------------------------------------
    # Shared pipeline helpers
    # ------------------------------------------------------------------

    def _parse_and_rewrite(
        self, source: str
    ) -> tuple[ast.Module, Rewriter] | ExecResult:
        """Parse source and run the AST rewriter.

        Returns ``(tree, rewriter)`` on success, or an ``ExecResult``
        with the error already populated on parse/validation failure.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return ExecResult(error=e)

        wrapped_mode = self.mode == "wrapped"
        rewriter = Rewriter(wrapped_mode=wrapped_mode)
        try:
            tree = rewriter.visit(tree)
        except StValidationError as e:
            return ExecResult(error=e)

        return tree, rewriter

    def _compile_and_setup(
        self,
        tree: ast.Module,
        rewriter: Rewriter,
        source: str,
        namespace: Mapping[str, Any] | None,
    ) -> tuple[
        Any,
        str,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        TailBuffer,
        list[tuple[Any, ...]] | None,
    ]:
        """Fix locations, register linecache, compile, and build namespace.

        Returns ``(code, filename, gates, ns, injected, stdout_buf, prints_list)``.
        """
        ast.fix_missing_locations(tree)

        filename = f"<sandtrap:{next(_exec_counter)}>"
        lines = source.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        linecache.cache[filename] = (
            len(source),
            None,
            lines,
            filename,
        )

        code = compile(tree, filename, "exec")

        wrapped_mode = self.mode == "wrapped"
        self._cancel_flag.clear()
        mem_limit_bytes, start_rss = self._memory_params()
        gates = make_gates(
            self.policy,
            _start_time=time.monotonic(),
            _cancel_flag=self._cancel_flag,
            _func_asts=rewriter._func_asts if wrapped_mode else None,
            _class_asts=rewriter._class_asts if wrapped_mode else None,
            _wrapped_mode=wrapped_mode,
            _memory_limit_bytes=mem_limit_bytes,
            _start_rss=start_rss,
            _filesystem=self.filesystem,
        )
        stdout_buf = self._make_stdout_buf()
        prints_list = [] if self.snapshot_prints else None
        ns, injected = self._build_namespace(namespace, gates, stdout_buf, prints_list)
        self._auto_activate(ns, gates)

        return code, filename, gates, ns, injected, stdout_buf, prints_list

    def _build_result(
        self,
        ns: dict[str, Any],
        injected: dict[str, Any],
        gates: dict[str, Any],
        stdout_buf: TailBuffer,
        prints_list: list[tuple[Any, ...]] | None,
        filename: str,
        error: BaseException | None,
        extra_locals: dict[str, Any] | None = None,
    ) -> ExecResult:
        """Post-execution: attach refs, build result namespace, clean up."""
        self._attach_sandbox_refs(ns, gates)

        result_ns = {
            k: v
            for k, v in ns.items()
            if k not in _INTERNAL_KEYS
            and not k.startswith("__st_")
            and not (k in injected and v is injected[k])
        }

        if extra_locals:
            for k, v in extra_locals.items():
                if not k.startswith("__st_"):
                    result_ns[k] = v

        tick_counter = gates.get("__st_tick_counter__")
        ticks = tick_counter[0] if tick_counter else 0

        linecache.cache.pop(filename, None)

        return ExecResult(
            namespace=result_ns,
            stdout=stdout_buf.getvalue(),
            error=error,
            ticks=ticks,
            prints=prints_list or [],
        )

    # ------------------------------------------------------------------
    # Public execution methods
    # ------------------------------------------------------------------

    def exec(
        self,
        source: str,
        *,
        namespace: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        """Execute source code synchronously in the sandbox."""
        prepared = self._parse_and_rewrite(source)
        if isinstance(prepared, ExecResult):
            return prepared
        tree, rewriter = prepared

        code, filename, gates, ns, injected, stdout_buf, prints_list = (
            self._compile_and_setup(tree, rewriter, source, namespace)
        )

        error = None
        with ExitStack() as stack:
            self._enter_sandbox_context(stack)
            try:
                exec(code, ns)  # noqa: S102
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise  # Real Ctrl-C from host
                error = strip_internal_frames(e)

        gates["__st_in_exec__"][0] = False

        return self._build_result(
            ns, injected, gates, stdout_buf, prints_list, filename, error
        )

    def activate(
        self,
        obj: Any,
        *,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        """Activate an unpickled StFunction/StClass/StInstance."""
        gates = make_gates(self.policy)
        if isinstance(obj, StFunction):
            obj.activate(gates, sandbox=self, namespace=namespace)
        elif isinstance(obj, StClass):
            obj.activate(gates, sandbox=self, namespace=namespace)
        elif isinstance(obj, StInstance):
            sb_class = object.__getattribute__(obj, "_st_class")
            if sb_class._compiled_cls is None:
                sb_class.activate(gates, sandbox=self, namespace=namespace)
            obj.activate(gates=gates, sandbox=self, namespace=namespace)
        else:
            raise TypeError(f"Cannot activate {type(obj).__name__}")

    async def aexec(
        self,
        source: str,
        *,
        namespace: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        """Execute source code asynchronously in the sandbox."""
        prepared = self._parse_and_rewrite(source)
        if isinstance(prepared, ExecResult):
            return prepared
        tree, rewriter = prepared

        # Wrap body in:
        #   async def __st_aexec__():
        #       try:
        #           <body>
        #       except BaseException:
        #           __st_local_capture__.update(__st_locals__())
        #           raise
        #       return __st_locals__()
        #
        # The try/except ensures locals are captured even when the body
        # raises (e.g. TaskContinue, TaskSuccess) before the return.
        return_locals = ast.Return(
            value=ast.Call(
                func=ast.Name(id="__st_locals__", ctx=ast.Load()),
                args=[],
                keywords=[],
            )
        )
        capture_locals_on_error = ast.ExceptHandler(
            type=None,
            name=None,
            body=[
                ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="__st_local_capture__", ctx=ast.Load()),
                            attr="update",
                            ctx=ast.Load(),
                        ),
                        args=[
                            ast.Call(
                                func=ast.Name(id="__st_locals__", ctx=ast.Load()),
                                args=[],
                                keywords=[],
                            )
                        ],
                        keywords=[],
                    )
                ),
                ast.Raise(),
            ],
        )
        try_wrapper = ast.Try(
            body=tree.body,
            handlers=[capture_locals_on_error],
            orelse=[],
            finalbody=[],
        )
        wrapper_kwargs: dict[str, Any] = dict(
            name="__st_aexec__",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=[try_wrapper, return_locals],
            decorator_list=[],
            returns=None,
        )
        # type_params added in Python 3.12
        if hasattr(ast.AsyncFunctionDef, "type_params"):
            wrapper_kwargs["type_params"] = []
        wrapper = ast.AsyncFunctionDef(**wrapper_kwargs)
        tree.body = [wrapper]

        code, filename, gates, ns, injected, stdout_buf, prints_list = (
            self._compile_and_setup(tree, rewriter, source, namespace)
        )
        ns["__st_locals__"] = _builtins.locals
        ns["__st_local_capture__"] = {}

        error = None
        result_locals: dict[str, Any] = {}
        with ExitStack() as stack:
            self._enter_sandbox_context(stack)
            try:
                exec(code, ns)  # noqa: S102
                coro = ns["__st_aexec__"]()
                result_locals = await asyncio.wait_for(
                    coro, timeout=self.policy.timeout
                )
                if result_locals is None:
                    result_locals = {}
            except asyncio.TimeoutError:
                error = StTimeout(f"Execution exceeded {self.policy.timeout}s timeout")
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise  # Real Ctrl-C from host
                error = strip_internal_frames(e)
                result_locals = ns.get("__st_local_capture__", {})

        gates["__st_in_exec__"][0] = False

        return self._build_result(
            ns,
            injected,
            gates,
            stdout_buf,
            prints_list,
            filename,
            error,
            extra_locals=result_locals,
        )
