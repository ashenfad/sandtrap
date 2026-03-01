"""Gate functions injected into sandboxed code at compile time."""

import ast
import asyncio
import functools
import posixpath
import string as _string_mod
import sys
import threading
import time
import types
from contextlib import ExitStack
from typing import Any, cast

from .builtins import make_safe_builtins
from .errors import StCancelled, StTickLimit, StTimeout
from .fs import current_fs, suspend
from .net.context import allow_network, network_allowed
from .policy import Policy
from .resource_limits import get_rss_bytes
from .rewriter import Rewriter
from .wrappers import StClass, StFunction, StInstance


class _SafeFormatter(_string_mod.Formatter):
    """Formatter that blocks attribute and item traversal in field names.

    Standard ``str.format`` allows ``"{0.__class__}".format(obj)`` which
    performs attribute access outside the AST rewriter's reach.  This
    subclass overrides ``get_field`` to reject any such traversal.
    """

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        if "." in field_name or "[" in field_name:
            raise AttributeError(
                "Attribute/item access in format strings is not allowed"
            )
        return super().get_field(field_name, args, kwargs)


_safe_formatter = _SafeFormatter()


def wrap_privileged(
    fn: Any,
    *,
    network_access: bool = False,
    host_fs_access: bool = False,
) -> Any:
    """Wrap a callable to temporarily grant network/fs privileges."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with ExitStack() as stack:
            if network_access:
                stack.enter_context(allow_network())
            if host_fs_access:
                stack.enter_context(suspend())
            return fn(*args, **kwargs)

    return wrapper


def _capture_context(fn: Any) -> Any:
    """Wrap a callable to restore sandbox ContextVars when called outside exec.

    Used in raw mode to ensure that callbacks (NiceGUI on_click, on_change, etc.)
    retain filesystem and network isolation even though they fire in a different
    asyncio Task after sb.exec() has returned and its ContextVars have reset.

    Captures the current ``current_fs`` and ``network_allowed`` values at
    decoration time and restores them on every call.
    """
    captured_fs = current_fs.get(None)
    captured_net = network_allowed.get()

    # No restrictions active — skip wrapping entirely.
    if captured_fs is None and captured_net:
        return fn

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            tok_fs = current_fs.set(captured_fs) if captured_fs is not None else None
            tok_net = network_allowed.set(captured_net)
            try:
                return await fn(*args, **kwargs)
            finally:
                network_allowed.reset(tok_net)
                if tok_fs is not None:
                    current_fs.reset(tok_fs)

        return async_wrapper
    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tok_fs = current_fs.set(captured_fs) if captured_fs is not None else None
            tok_net = network_allowed.set(captured_net)
            try:
                return fn(*args, **kwargs)
            finally:
                network_allowed.reset(tok_net)
                if tok_fs is not None:
                    current_fs.reset(tok_fs)

        return wrapper


class _VFSLoader:
    """Resolves and caches modules from a virtual filesystem.

    Handles parsing, rewriting, compiling, and executing VFS source files
    into module objects, including package chains for dotted imports.
    """

    def __init__(
        self,
        filesystem: Any,
        wrapped_mode: bool,
        gates: dict[str, Any],
    ) -> None:
        self._filesystem = filesystem
        self._wrapped_mode = wrapped_mode
        self._gates = gates
        self._cache: dict[str, Any] = {}

    def _compile_and_exec(self, mod: Any, source: str, module_name: str) -> None:
        """Parse, rewrite, compile, and execute VFS source into a module."""
        tree = ast.parse(source)
        rewriter = Rewriter(wrapped_mode=self._wrapped_mode)
        tree = rewriter.visit(tree)
        ast.fix_missing_locations(tree)
        code = compile(tree, f"<sandtrap:vfs:{module_name}>", "exec")

        ns = dict(mod.__dict__)
        ns["__builtins__"] = make_safe_builtins(
            self._gates["__st_getattr__"],
            checkpoint=self._gates["__st_checkpoint__"],
        )
        ns.update(self._gates)

        # Override defun/defclass gates with VFS-specific ones that
        # reference this rewriter's AST lists (not the main code's)
        if self._wrapped_mode and rewriter._func_asts:
            vfs_func_asts = rewriter._func_asts

            def _vfs_defun(name: str, compiled_fn: Any, ast_ref: int | str) -> Any:
                if isinstance(ast_ref, str):
                    func_ast = cast(ast.FunctionDef, ast.parse(ast_ref).body[0])
                else:
                    func_ast = vfs_func_asts[ast_ref]
                return StFunction(name, compiled_fn, func_ast)

            ns["__st_defun__"] = _vfs_defun

        if self._wrapped_mode and rewriter._class_asts:
            vfs_class_asts = rewriter._class_asts
            getattr_gate = self._gates["__st_getattr__"]

            def _vfs_defclass(
                name: str, compiled_cls: Any, ast_idx: int, **frozen_refs: Any
            ) -> Any:
                cls_ast = vfs_class_asts[ast_idx]
                sb_cls = StClass(name, compiled_cls, cls_ast, frozen_refs=frozen_refs)
                sb_cls._st_getattr_gate = getattr_gate
                return sb_cls

            ns["__st_defclass__"] = _vfs_defclass

        exec(code, ns)  # noqa: S102

        # Update module dict (strip internal keys)
        for k, v in ns.items():
            if k != "__builtins__" and not k.startswith("__st_"):
                setattr(mod, k, v)

    def resolve_module(self, module_name: str) -> Any:
        """Try to resolve a module from the VFS.  Returns None if not found."""
        if self._filesystem is None:
            return None

        if module_name in self._cache:
            return self._cache[module_name]

        # Look for /<module_name>.py in the VFS (dots → path separators)
        path = "/" + module_name.replace(".", "/") + ".py"
        if not self._filesystem.exists(path):
            return None

        with self._filesystem.open(path, "r") as f:
            source = f.read()

        # Cache before execution (circular import protection)
        mod = types.ModuleType(module_name)
        mod.__file__ = path
        self._cache[module_name] = mod

        try:
            self._compile_and_exec(mod, source, module_name)
        except BaseException:
            self._cache.pop(module_name, None)
            raise

        return mod

    def ensure_package_chain(self, module_name: str) -> Any:
        """Build the parent package chain for dotted VFS imports.

        ``import pkg.mod`` must bind ``pkg`` in the namespace with
        ``pkg.mod`` attached as an attribute — matching standard Python
        import semantics.
        """
        parts = module_name.split(".")
        if len(parts) <= 1:
            return self.resolve_module(module_name)

        # Resolve the leaf module first
        leaf = self.resolve_module(module_name)
        if leaf is None:
            return None

        # Build parent packages from top down
        parent = None
        for i in range(len(parts) - 1):
            pkg_name = ".".join(parts[: i + 1])
            if pkg_name in self._cache:
                parent = self._cache[pkg_name]
            else:
                init_path = "/" + pkg_name.replace(".", "/") + "/__init__.py"
                pkg = types.ModuleType(pkg_name)
                pkg.__file__ = init_path
                pkg.__path__ = ["/" + pkg_name.replace(".", "/")]
                self._cache[pkg_name] = pkg

                if self._filesystem is not None and self._filesystem.exists(init_path):
                    with self._filesystem.open(init_path, "r") as f:
                        source = f.read()
                    try:
                        self._compile_and_exec(pkg, source, pkg_name)
                    except BaseException:
                        self._cache.pop(pkg_name, None)
                        raise

                parent = pkg

            # Attach child to parent
            if i > 0:
                prev_pkg_name = ".".join(parts[:i])
                prev_pkg = self._cache.get(prev_pkg_name)
                if prev_pkg is not None:
                    setattr(prev_pkg, parts[i], parent)

        # Attach leaf to its immediate parent
        if parent is not None:
            setattr(parent, parts[-1], leaf)

        return self._cache[parts[0]]


def make_gates(
    policy: Policy,
    *,
    _start_time: float | None = None,
    _cancel_flag: threading.Event | None = None,
    _func_asts: list | None = None,
    _class_asts: list | None = None,
    _wrapped_mode: bool = False,
    _memory_limit_bytes: int | None = None,
    _start_rss: int | None = None,
    _filesystem: Any = None,
) -> dict[str, Any]:
    """Create the set of gate functions for a given policy.

    Returns a dict of gate function names to implementations,
    suitable for injection into the execution namespace.
    """
    # Gate dict — populated at the end, but closures reference it so
    # VFS module compilation can inject the same gates.
    gates: dict[str, Any] = {}

    # VFS module loader (holds its own cache; reads gates dict by reference)
    vfs = _VFSLoader(_filesystem, _wrapped_mode, gates)

    def _unwrap(obj: Any) -> Any:
        """Unwrap StInstance to access the real underlying instance."""
        if isinstance(obj, StInstance):
            real = object.__getattribute__(obj, "_st_instance")
            if real is not None:
                return real
        return obj

    def _caller_lineno(depth: int = 2) -> int | None:
        """Get the line number of the sandboxed code that triggered a gate."""
        try:
            return sys._getframe(depth).f_lineno
        except (ValueError, AttributeError):
            return None

    def __st_getattr__(obj: Any, attr: str) -> Any:
        obj = _unwrap(obj)
        if not policy.is_attr_allowed(obj, attr):
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise AttributeError(
                f"Attribute '{attr}' is not accessible on '{type(obj).__name__}'{loc}"
            )

        # Intercept str.format / str.format_map to block field traversal
        if isinstance(obj, str) and attr in ("format", "format_map"):
            if attr == "format":

                def safe_format(*args: Any, **kwargs: Any) -> str:
                    return _safe_formatter.vformat(obj, args, kwargs)

                return safe_format
            else:

                def safe_format_map(mapping: Any) -> str:
                    return _safe_formatter.vformat(obj, (), mapping)

                return safe_format_map

        value = getattr(obj, attr)

        # Wrap callables from privileged registrations
        if callable(value):
            reg = policy._find_registration_for(obj)
            if reg is not None:
                needs_network = getattr(reg, "network_access", False)
                needs_host_fs = getattr(reg, "host_fs_access", False)
                # Check per-member overrides
                if hasattr(reg, "configure") and attr in reg.configure:
                    spec = reg.configure[attr]
                    needs_network = needs_network or spec.network_access
                    needs_host_fs = needs_host_fs or spec.host_fs_access
                if needs_network or needs_host_fs:
                    value = wrap_privileged(
                        value,
                        network_access=needs_network,
                        host_fs_access=needs_host_fs,
                    )

        return value

    def __st_setattr__(obj: Any, attr: str, value: Any) -> None:
        obj = _unwrap(obj)
        if not policy.is_attr_allowed(obj, attr):
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise AttributeError(
                f"Cannot set attribute '{attr}' on '{type(obj).__name__}'{loc}"
            )
        setattr(obj, attr, value)

    def __st_delattr__(obj: Any, attr: str) -> None:
        obj = _unwrap(obj)
        if not policy.is_attr_allowed(obj, attr):
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise AttributeError(
                f"Cannot delete attribute '{attr}' on '{type(obj).__name__}'{loc}"
            )
        delattr(obj, attr)

    def __st_import__(module_name: str, *, alias: str | None = None) -> Any:
        # Try policy-registered modules first
        if policy.is_import_allowed(module_name):
            if alias is not None:
                return policy.resolve_module(module_name)
            top_level = module_name.split(".")[0]
            return policy.resolve_module(top_level)

        # Try VFS modules (with package chain for dotted imports)
        if alias is not None or "." not in module_name:
            mod = vfs.resolve_module(module_name)
        else:
            mod = vfs.ensure_package_chain(module_name)
        if mod is not None:
            return mod

        lineno = _caller_lineno()
        loc = f" (line {lineno})" if lineno else ""
        raise ImportError(f"Import of '{module_name}' is not allowed{loc}")

    def __st_importfrom__(module_name: str, name: str, *, _level: int = 0) -> Any:
        if _level > 0:
            # Relative import — resolve against caller's __file__
            caller_file = sys._getframe(1).f_globals.get("__file__", "")
            base_dir = posixpath.dirname(caller_file)
            for _ in range(_level - 1):
                base_dir = posixpath.dirname(base_dir)

            if module_name:
                # from .foo import bar → resolve foo relative to base_dir
                abs_path = base_dir + "/" + module_name.replace(".", "/")
                abs_module = abs_path.lstrip("/").replace("/", ".")
            else:
                # from . import bar → treat bar as a sub-module of base_dir
                abs_parts = base_dir.strip("/")
                abs_module = (
                    (abs_parts.replace("/", ".") + "." + name) if abs_parts else name
                )

            mod = vfs.resolve_module(abs_module if module_name else abs_module)
            if mod is not None:
                if not module_name:
                    # from . import bar → return the module itself
                    return mod
                if hasattr(mod, name):
                    return getattr(mod, name)
                raise ImportError(f"cannot import name '{name}' from '{abs_module}'")
            raise ImportError(
                f"No module named '{abs_module}' (resolved from relative import)"
            )

        # Try policy first
        if policy.is_import_allowed(module_name):
            return policy.resolve_module_member(module_name, name)

        # Try VFS modules
        mod = vfs.resolve_module(module_name)
        if mod is not None:
            if hasattr(mod, name):
                return getattr(mod, name)
            # name might be a sub-module (from pkg import sub)
            sub = vfs.resolve_module(module_name + "." + name)
            if sub is not None:
                return sub
            raise ImportError(f"cannot import name '{name}' from '{module_name}'")

        # module_name might be a package directory without __init__.py
        sub = vfs.resolve_module(module_name + "." + name)
        if sub is not None:
            return sub

        lineno = _caller_lineno()
        loc = f" (line {lineno})" if lineno else ""
        raise ImportError(f"Import of '{module_name}' is not allowed{loc}")

    def __st_defun__(name: str, compiled_fn: Any, ast_ref: int | str) -> Any:
        if not _wrapped_mode:
            return compiled_fn

        if isinstance(ast_ref, str):
            # Inner function: ast_ref is source string embedded by rewriter
            func_ast = cast(ast.FunctionDef, ast.parse(ast_ref).body[0])
        else:
            if _func_asts is None:
                return compiled_fn
            func_ast = _func_asts[ast_ref]
        return StFunction(name, compiled_fn, func_ast)

    def __st_defclass__(
        name: str, compiled_cls: Any, ast_idx: int, **frozen_refs: Any
    ) -> Any:
        if not _wrapped_mode or _class_asts is None:
            return compiled_cls

        class_ast = _class_asts[ast_idx]
        sb_cls = StClass(name, compiled_cls, class_ast, frozen_refs=frozen_refs)
        sb_cls._st_getattr_gate = __st_getattr__
        return sb_cls

    # Mutable boxes so checkpoint state can be reset for direct calls
    _tick_counter = [0]
    _start_time_box = [_start_time]
    _cancel_flag_box = [_cancel_flag]
    _memory_box = [_memory_limit_bytes, _start_rss]

    def __st_checkpoint__() -> None:
        if _cancel_flag_box[0] is not None and _cancel_flag_box[0].is_set():
            raise StCancelled("Execution cancelled")
        _tick_counter[0] += 1
        if policy.tick_limit is not None and _tick_counter[0] > policy.tick_limit:
            raise StTickLimit(f"Execution exceeded {policy.tick_limit} tick limit")
        if _start_time_box[0] is not None and policy.timeout is not None:
            if time.monotonic() - _start_time_box[0] > policy.timeout:
                raise StTimeout(f"Execution exceeded {policy.timeout}s timeout")
        if _memory_box[0] is not None and _memory_box[1] is not None:
            if get_rss_bytes() - _memory_box[1] > _memory_box[0]:
                raise MemoryError(
                    f"Execution exceeded {policy.memory_limit}MB memory limit"
                )

    gates["__st_tick_counter__"] = _tick_counter
    gates["__st_start_time__"] = _start_time_box
    gates["__st_cancel_flag__"] = _cancel_flag_box
    gates["__st_memory__"] = _memory_box
    gates.update(
        {
            "__st_getattr__": __st_getattr__,
            "__st_setattr__": __st_setattr__,
            "__st_delattr__": __st_delattr__,
            "__st_import__": __st_import__,
            "__st_importfrom__": __st_importfrom__,
            "__st_defun__": __st_defun__,
            "__st_defclass__": __st_defclass__,
            "__st_checkpoint__": __st_checkpoint__,
            "__st_capture_context__": _capture_context,
        }
    )
    return gates
