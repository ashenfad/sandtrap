"""Gate functions injected into sandboxed code at compile time."""

import functools
import string as _string_mod
import threading
import time
from contextlib import ExitStack
from typing import Any

from .errors import SbCancelled, SbTickLimit, SbTimeout
from .policy import Policy


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


def _wrap_privileged(
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
                from .net.context import allow_network

                stack.enter_context(allow_network())
            if host_fs_access:
                from .fs.context import suspend_fs_interception

                stack.enter_context(suspend_fs_interception())
            return fn(*args, **kwargs)

    return wrapper


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
    from .wrappers import SbInstance

    # Gate dict — populated at the end, but closures reference it so
    # VFS module compilation can inject the same gates.
    gates: dict[str, Any] = {}

    # Per-execution module cache for VFS imports.
    _vfs_module_cache: dict[str, Any] = {}

    def _unwrap(obj: Any) -> Any:
        """Unwrap SbInstance to access the real underlying instance."""
        if isinstance(obj, SbInstance):
            real = object.__getattribute__(obj, "_sb_instance")
            if real is not None:
                return real
        return obj

    def _resolve_vfs_module(module_name: str) -> Any:
        """Try to resolve a module from the VFS.  Returns None if not found."""
        if _filesystem is None:
            return None

        if module_name in _vfs_module_cache:
            return _vfs_module_cache[module_name]

        # Look for /<module_name>.py in the VFS (dots → path separators)
        path = "/" + module_name.replace(".", "/") + ".py"
        if not _filesystem.exists(path):
            return None

        import ast as _ast
        import types

        from .builtins import SAFE_BUILTINS
        from .rewriter import Rewriter

        # Read source
        f = _filesystem.open(path, "r")
        source = f.read()
        f.close()

        # Create module object and cache it before execution (circular import protection)
        mod = types.ModuleType(module_name)
        mod.__file__ = path
        _vfs_module_cache[module_name] = mod

        try:
            # Parse + rewrite (wrapped mode wraps functions/classes as SbFunction/SbClass)
            tree = _ast.parse(source)
            rewriter = Rewriter(wrapped_mode=_wrapped_mode)
            tree = rewriter.visit(tree)
            _ast.fix_missing_locations(tree)
            code = compile(tree, f"<sblite:vfs:{module_name}>", "exec")

            # Build namespace with same gates
            ns = dict(mod.__dict__)
            ns["__builtins__"] = dict(SAFE_BUILTINS)
            ns.update(gates)

            # Override defun/defclass gates with VFS-specific ones that
            # reference this rewriter's AST lists (not the main code's)
            if _wrapped_mode and rewriter._func_asts:
                vfs_func_asts = rewriter._func_asts

                def _vfs_defun(name: str, compiled_fn: Any, ast_ref: int | str) -> Any:
                    from .wrappers import SbFunction

                    if isinstance(ast_ref, str):
                        import ast as _ast
                        from typing import cast

                        func_ast = cast(_ast.FunctionDef, _ast.parse(ast_ref).body[0])
                    else:
                        func_ast = vfs_func_asts[ast_ref]
                    return SbFunction(name, compiled_fn, func_ast)

                ns["__sb_defun__"] = _vfs_defun

            if _wrapped_mode and rewriter._class_asts:
                vfs_class_asts = rewriter._class_asts

                def _vfs_defclass(
                    name: str, compiled_cls: Any, ast_idx: int, **frozen_refs: Any
                ) -> Any:
                    from .wrappers import SbClass

                    cls_ast = vfs_class_asts[ast_idx]
                    sb_cls = SbClass(name, compiled_cls, cls_ast, frozen_refs=frozen_refs)
                    sb_cls._sb_getattr_gate = __sb_getattr__
                    return sb_cls

                ns["__sb_defclass__"] = _vfs_defclass

            # Execute module code
            exec(code, ns)  # noqa: S102
        except BaseException:
            # Evict broken module so subsequent imports can retry
            _vfs_module_cache.pop(module_name, None)
            raise

        # Update module dict (strip internal keys)
        for k, v in ns.items():
            if k != "__builtins__" and not k.startswith("__sb_"):
                setattr(mod, k, v)

        return mod

    def __sb_getattr__(obj: Any, attr: str) -> Any:
        obj = _unwrap(obj)
        if not policy.is_attr_allowed(obj, attr):
            raise AttributeError(
                f"'{type(obj).__name__}' has no attribute '{attr}'"
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
                    value = _wrap_privileged(
                        value,
                        network_access=needs_network,
                        host_fs_access=needs_host_fs,
                    )

        return value

    def __sb_setattr__(obj: Any, attr: str, value: Any) -> None:
        obj = _unwrap(obj)
        if not policy.is_attr_allowed(obj, attr):
            raise AttributeError(
                f"cannot set attribute '{attr}' on '{type(obj).__name__}'"
            )
        setattr(obj, attr, value)

    def __sb_delattr__(obj: Any, attr: str) -> None:
        obj = _unwrap(obj)
        if not policy.is_attr_allowed(obj, attr):
            raise AttributeError(
                f"cannot delete attribute '{attr}' on '{type(obj).__name__}'"
            )
        delattr(obj, attr)

    def __sb_import__(module_name: str, *, alias: str | None = None) -> Any:
        # Try policy-registered modules first
        if policy.is_import_allowed(module_name):
            if alias is not None:
                return policy.resolve_module(module_name)
            top_level = module_name.split(".")[0]
            return policy.resolve_module(top_level)

        # Try VFS modules
        mod = _resolve_vfs_module(module_name)
        if mod is not None:
            return mod

        raise ImportError(f"Import of '{module_name}' is not allowed")

    def __sb_importfrom__(module_name: str, name: str, *, _level: int = 0) -> Any:
        if _level > 0:
            # Relative import — resolve against caller's __file__
            import posixpath
            import sys

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
                abs_module = (abs_parts.replace("/", ".") + "." + name) if abs_parts else name

            mod = _resolve_vfs_module(abs_module if module_name else abs_module)
            if mod is not None:
                if not module_name:
                    # from . import bar → return the module itself
                    return mod
                if hasattr(mod, name):
                    return getattr(mod, name)
                raise ImportError(
                    f"cannot import name '{name}' from '{abs_module}'"
                )
            raise ImportError(
                f"No module named '{abs_module}' (resolved from relative import)"
            )

        # Try policy first
        if policy.is_import_allowed(module_name):
            return policy.resolve_module_member(module_name, name)

        # Try VFS modules
        mod = _resolve_vfs_module(module_name)
        if mod is not None:
            if hasattr(mod, name):
                return getattr(mod, name)
            # name might be a sub-module (from pkg import sub)
            sub = _resolve_vfs_module(module_name + "." + name)
            if sub is not None:
                return sub
            raise ImportError(
                f"cannot import name '{name}' from '{module_name}'"
            )

        # module_name might be a package directory without __init__.py
        sub = _resolve_vfs_module(module_name + "." + name)
        if sub is not None:
            return sub

        raise ImportError(f"Import of '{module_name}' is not allowed")

    def __sb_defun__(name: str, compiled_fn: Any, ast_ref: int | str) -> Any:
        if not _wrapped_mode:
            return compiled_fn
        from .wrappers import SbFunction

        if isinstance(ast_ref, str):
            # Inner function: ast_ref is source string embedded by rewriter
            import ast as _ast
            from typing import cast

            func_ast = cast(_ast.FunctionDef, _ast.parse(ast_ref).body[0])
        else:
            if _func_asts is None:
                return compiled_fn
            func_ast = _func_asts[ast_ref]
        return SbFunction(name, compiled_fn, func_ast)

    def __sb_defclass__(
        name: str, compiled_cls: Any, ast_idx: int, **frozen_refs: Any
    ) -> Any:
        if not _wrapped_mode or _class_asts is None:
            return compiled_cls
        from .wrappers import SbClass

        class_ast = _class_asts[ast_idx]
        sb_cls = SbClass(name, compiled_cls, class_ast, frozen_refs=frozen_refs)
        sb_cls._sb_getattr_gate = __sb_getattr__
        return sb_cls

    # Mutable boxes so checkpoint state can be reset for direct calls
    _tick_counter = [0]
    _start_time_box = [_start_time]
    _cancel_flag_box = [_cancel_flag]
    _memory_box = [_memory_limit_bytes, _start_rss]

    def __sb_checkpoint__() -> None:
        if _cancel_flag_box[0] is not None and _cancel_flag_box[0].is_set():
            raise SbCancelled("Execution cancelled")
        _tick_counter[0] += 1
        if policy.tick_limit is not None and _tick_counter[0] > policy.tick_limit:
            raise SbTickLimit(
                f"Execution exceeded {policy.tick_limit} tick limit"
            )
        if _start_time_box[0] is not None and policy.timeout is not None:
            if time.monotonic() - _start_time_box[0] > policy.timeout:
                raise SbTimeout(
                    f"Execution exceeded {policy.timeout}s timeout"
                )
        if _memory_box[0] is not None and _memory_box[1] is not None:
            from .resource_limits import get_rss_bytes

            if get_rss_bytes() - _memory_box[1] > _memory_box[0]:
                raise MemoryError(
                    f"Execution exceeded {policy.memory_limit}MB memory limit"
                )

    gates["__sb_tick_counter__"] = _tick_counter
    gates["__sb_start_time__"] = _start_time_box
    gates["__sb_cancel_flag__"] = _cancel_flag_box
    gates["__sb_memory__"] = _memory_box
    gates.update({
        "__sb_getattr__": __sb_getattr__,
        "__sb_setattr__": __sb_setattr__,
        "__sb_delattr__": __sb_delattr__,
        "__sb_import__": __sb_import__,
        "__sb_importfrom__": __sb_importfrom__,
        "__sb_defun__": __sb_defun__,
        "__sb_defclass__": __sb_defclass__,
        "__sb_checkpoint__": __sb_checkpoint__,
    })
    return gates
