"""Gate functions injected into sandboxed code at compile time."""

import ast
import builtins as _builtins
import functools
import importlib.util
import inspect
import posixpath
import string as _string_mod
import sys
import threading
import time
import types
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any, cast

from .builtins import _FrozenBuiltins, make_safe_builtins
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

# Distinguishes "the module has no such export" from "its value is None".
_MISSING = object()


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


def _module_export(mod: Any, name: str) -> Any:
    """The value a module's body bound to *name*, or ``_MISSING``.

    Resolution goes through the module's own ``__dict__``, never
    ``getattr``: ``getattr`` walks the type, so it answers for names the
    module never defined -- ``__getattribute__`` (which reads any
    attribute, including the module dict the sandbox's gates live in),
    ``__dict__`` (that dict itself), ``__class__``, and the rest of the
    module type's surface.

    Dunders are refused whatever their source. An implementation dunder
    is type machinery wearing a module's name, and a dunder a module
    *body* assigned is a name the attribute gate already refuses to
    read, so honouring it here would only make the two spellings
    disagree about the same name.
    """
    if name.startswith("__") and name.endswith("__"):
        return _MISSING
    return mod.__dict__.get(name, _MISSING)


class _ExecModule(types.ModuleType):
    """A module the embedder handed to a single execution.

    The embedder chose the contents, so every name it was given is
    readable -- the policy's member filters describe what sandboxed code
    may reach on a *registered* module, and have nothing to say about a
    mapping the embedder wrote out by hand. Nothing else on the object is
    readable, and nothing at all is writable: a per-exec module that
    sandboxed code could write to would be a channel from one execution
    to the next, which is exactly what per-exec means to rule out.
    """

    __slots__ = ("_st_exposed",)

    def __init__(self, name: str, attributes: Mapping[str, Any]) -> None:
        super().__init__(name)
        self.__dict__.update(attributes)
        object.__setattr__(self, "_st_exposed", frozenset(attributes))

    def _st_refuse_write(self, attr: str) -> "AttributeError":
        return AttributeError(
            f"Cannot set attribute '{attr}' on module '{self.__name__}': "
            "modules provided for this execution are read-only"
        )

    def __setattr__(self, attr: str, value: Any) -> None:
        raise self._st_refuse_write(attr)

    def __delattr__(self, attr: str) -> None:
        raise self._st_refuse_write(attr)

    def __dir__(self) -> list[str]:
        return sorted(self._st_exposed)


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
        root: str = "/",
    ) -> None:
        self._filesystem = filesystem
        self._wrapped_mode = wrapped_mode
        self._gates = gates
        self._cache: dict[str, Any] = {}
        self._print_fn: Any = None
        self._help_fn: Any = None
        self._input_fn: Any = None
        # Import-resolution base (Policy.module_root): "" for the fs
        # root so f"{self._root}/mod.py" composes to "/mod.py".
        self._root = "" if root == "/" else root.rstrip("/")

    def _compile_and_exec(self, mod: Any, source: str, module_name: str) -> None:
        """Parse, rewrite, compile, and execute VFS source into a module.

        A module's execution namespace IS its ``__dict__``.  The module's
        own functions and the caller holding the module object therefore
        read and write one dict: setting ``mod.LIMIT`` changes what
        ``mod.get()`` reads, and ``patch.object(mod, "fetch", fake)``
        is what ``mod`` itself calls.  Executing into a copy and
        assigning the results back would make the module a facade over a
        snapshot, where both of those silently do nothing.

        Sharing the dict also means a body that raises leaves the module
        half-populated, so a failed module must never stay importable:
        the callers drop it from the cache before re-raising, and the
        next import builds a fresh module object and runs the body
        again.

        The gates and builtins the module runs on live in that same dict
        and are unreachable from sandboxed code: the rewriter refuses
        ``__builtins__`` and ``__st_*`` as source-level names, the
        attribute gate refuses them as attributes, and the import gate
        refuses them as ``from <module> import`` targets.
        """
        tree = ast.parse(source)
        rewriter = Rewriter(wrapped_mode=self._wrapped_mode)
        tree = rewriter.visit(tree)
        ast.fix_missing_locations(tree)
        code = compile(tree, f"<sandtrap:vfs:{module_name}>", "exec")

        ns = mod.__dict__
        ns["__builtins__"] = make_safe_builtins(
            self._gates["__st_getattr__"],
            checkpoint=self._gates["__st_checkpoint__"],
        )
        # Provide __import__ so C extensions can import transitive deps.
        # User-code imports are gated at the AST level.
        ns["__builtins__"]["__import__"] = _builtins.__import__
        # Inject print/help/open so VFS modules can use them like top-level code.
        if self._print_fn is not None:
            ns["__builtins__"]["print"] = self._print_fn
        if self._help_fn is not None:
            ns["__builtins__"]["help"] = self._help_fn
        if self._input_fn is not None:
            ns["__builtins__"]["input"] = self._input_fn
        if self._filesystem is not None:
            ns["__builtins__"]["open"] = _builtins.open
        ns["__builtins__"] = _FrozenBuiltins(ns["__builtins__"])
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

    def find_module_file(self, top: str, max_dirs: int = 200) -> str | None:
        """Bounded BFS for ``<top>.py`` anywhere on the VFS — powers the
        did-you-mean in import errors. Skips the module root itself (a
        hit there would have resolved normally) and returns the
        shallowest match.

        Only descends into directories that could legally appear in a
        dotted import path (valid identifiers, non-dunder): a hit inside
        ``.git/``, ``__pycache__/``, or ``my-stuff/`` would be a
        non-actionable suggestion — and every skipped entry saves a
        round-trip when the filesystem is RPC-bridged (RemoteFS)."""
        fs = self._filesystem
        if fs is None:
            return None
        target = f"{top}.py"
        module_root = self._root or "/"
        queue = ["/"]
        visited = 0
        while queue and visited < max_dirs:
            d = queue.pop(0)
            visited += 1
            try:
                entries = sorted(fs.list(d))
            except Exception:
                continue
            for entry in entries:
                if entry == target and d.rstrip("/") != module_root.rstrip("/"):
                    return d.rstrip("/") + "/" + entry
                if not entry.isidentifier() or (
                    entry.startswith("__") and entry.endswith("__")
                ):
                    continue  # can't ride a dotted import; skip the isdir
                full = d.rstrip("/") + "/" + entry
                try:
                    if fs.isdir(full):
                        queue.append(full)
                except Exception:
                    continue
        return None

    def resolve_module(self, module_name: str) -> Any:
        """Try to resolve a module from the VFS.  Returns None if not found."""
        if self._filesystem is None:
            return None

        if module_name in self._cache:
            return self._cache[module_name]

        # Look for <root>/<module_name>.py in the VFS (dots → path
        # separators; root is Policy.module_root, default the fs root)
        path = self._root + "/" + module_name.replace(".", "/") + ".py"
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
                init_path = (
                    self._root + "/" + pkg_name.replace(".", "/") + "/__init__.py"
                )
                pkg = types.ModuleType(pkg_name)
                pkg.__file__ = init_path
                pkg.__path__ = [self._root + "/" + pkg_name.replace(".", "/")]
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
    _sandbox_sys: Any = None,
) -> dict[str, Any]:
    """Create the set of gate functions for a given policy.

    Returns a dict of gate function names to implementations,
    suitable for injection into the execution namespace.
    """
    # Gate dict — populated at the end, but closures reference it so
    # VFS module compilation can inject the same gates.
    gates: dict[str, Any] = {}

    # Modules the embedder handed to this execution, by name. Filled in
    # once the namespace is built (the attribute values are materialized
    # there, so under worker isolation they are live proxies rather than
    # markers) and emptied when the execution ends. Importable from
    # top-level code and from workspace modules alike, because both run
    # on these gates.
    exec_modules: dict[str, _ExecModule] = {}

    # VFS module loader (holds its own cache; reads gates dict by
    # reference). getattr: policies pickled by older versions may lack
    # module_root.
    vfs = _VFSLoader(
        _filesystem,
        _wrapped_mode,
        gates,
        root=getattr(policy, "module_root", "/"),
    )

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

    def _import_error_message(module_name: str) -> str:
        """Distinguish 'not granted' from 'exists, but not where imports
        resolve'. Filesystem-module imports resolve from the module root
        (Policy.module_root, default '/') — a bare ``import mod`` for a
        file at <root>/some/dir/mod.py is the single most common miss,
        and 'not allowed' misreads as a policy ban (agents give up
        instead of qualifying the import). When the file exists
        elsewhere on the VFS, say where and show the fix."""
        # bare `import mod`: search for mod.py. Dotted with a WRONG
        # root (`import api._helpers` for /app/api/_helpers.py): the
        # top segment is a directory, so search for the LEAF file and
        # derive the right root from where it lives.
        parts = module_name.split(".")
        hit = vfs.find_module_file(parts[0])
        if hit is None and len(parts) > 1:
            hit = vfs.find_module_file(parts[-1])
        module_root = vfs._root or "/"
        if hit is not None:
            rel = (
                hit[len(vfs._root) :]
                if vfs._root and hit.startswith(vfs._root + "/")
                else (hit if not vfs._root else None)
            )
            if rel is not None:
                dotted = rel[1:-3].replace("/", ".")
                parent, _, leaf = dotted.rpartition(".")
                return (
                    f"No module named '{module_name}' at the module root "
                    f"(imports of filesystem modules resolve from "
                    f"'{module_root}'). Found {hit} — try: "
                    f"from {parent} import {leaf}"
                )
            return (
                f"No module named '{module_name}' at the module root "
                f"(imports of filesystem modules resolve from "
                f"'{module_root}'). Found {hit}, which is OUTSIDE the "
                f"module root — move it under {module_root} to import it"
            )
        return f"Import of '{module_name}' is not allowed"

    def _maybe_wrap_privileged(value: Any, reg: Any, member_name: str) -> Any:
        """Wrap *value* if its registration requires network or host-fs access."""
        if not callable(value) or reg is None:
            return value
        needs_network = getattr(reg, "network_access", False)
        needs_host_fs = getattr(reg, "host_fs_access", False)
        # Check per-member overrides
        if hasattr(reg, "configure") and member_name in reg.configure:
            spec = reg.configure[member_name]
            needs_network = needs_network or spec.network_access
            needs_host_fs = needs_host_fs or spec.host_fs_access
        if needs_network or needs_host_fs:
            return wrap_privileged(
                value,
                network_access=needs_network,
                host_fs_access=needs_host_fs,
            )
        return value

    def __st_getattr__(obj: Any, attr: str) -> Any:
        obj = _unwrap(obj)
        if isinstance(obj, _ExecModule):
            # The module's surface is exactly the mapping the embedder
            # wrote, so the policy's member filters have nothing to say
            # about it and anything else simply is not there.
            if attr in obj._st_exposed:
                # Straight out of the instance dict, so a mapping key that
                # collides with the module type's own surface (`__dict__`,
                # `__class__`) yields the embedder's value rather than the
                # machinery `getattr` would reach past it for.
                return obj.__dict__[attr]
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise AttributeError(
                f"module '{obj.__name__}' has no attribute '{attr}'{loc}"
            )
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
        if callable(value):
            reg = policy._find_registration_for(obj)
            return _maybe_wrap_privileged(value, reg, attr)
        return value

    def __st_setattr__(obj: Any, attr: str, value: Any) -> None:
        obj = _unwrap(obj)
        if isinstance(obj, _ExecModule):
            raise obj._st_refuse_write(attr)
        if not policy.is_attr_allowed(obj, attr):
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise AttributeError(
                f"Cannot set attribute '{attr}' on '{type(obj).__name__}'{loc}"
            )
        setattr(obj, attr, value)

    def __st_delattr__(obj: Any, attr: str) -> None:
        obj = _unwrap(obj)
        if isinstance(obj, _ExecModule):
            raise obj._st_refuse_write(attr)
        if not policy.is_attr_allowed(obj, attr):
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise AttributeError(
                f"Cannot delete attribute '{attr}' on '{type(obj).__name__}'{loc}"
            )
        delattr(obj, attr)

    def __st_import__(
        module_name: str, *, alias: str | None = None, _depth: int = 0
    ) -> Any:
        # _depth offsets the caller-frame walks below by the number of extra
        # frames between user code and here (1 when __st_dynimport__ calls
        # through), so line numbers and the __main__ proxy still read the
        # sandboxed frame rather than an intermediate gate frame.
        # Synthetic safe `sys` (stdin/stdout/stderr/argv) when provided —
        # takes precedence over the policy so `import sys` returns it
        # rather than being blocked. Safe by construction (see SandboxSys).
        if _sandbox_sys is not None and module_name == "sys":
            return _sandbox_sys

        # Modules the embedder handed to this execution resolve ahead of
        # the policy allowlist. A name that collides with a grant is
        # refused when the execution starts, so the order settles only
        # which gate reports a name neither side owns.
        exec_mod = exec_modules.get(module_name)
        if exec_mod is not None:
            return exec_mod

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

        # "import main" / "import __main__" — return a namespace proxy so
        # that LLM-generated code like "from main import X" works when X is
        # already available in the sandbox globals.
        if module_name in ("main", "__main__"):
            caller_globals = sys._getframe(1 + _depth).f_globals
            proxy = types.ModuleType(module_name)
            proxy.__dict__.update(
                {
                    k: v
                    for k, v in caller_globals.items()
                    if not k.startswith("__st_") and k != "__builtins__"
                }
            )
            return proxy

        lineno = _caller_lineno(2 + _depth)
        loc = f" (line {lineno})" if lineno else ""
        raise ImportError(_import_error_message(module_name) + loc)

    def __st_importfrom__(
        module_name: str, name: str, *, _level: int = 0, _depth: int = 0
    ) -> Any:
        # Sandbox internals are never importable from anything, a granted
        # module included. `__builtins__` holds the real `__import__`
        # (parked there for C extensions) and the `__st_*` gates are the
        # enforcement machinery itself; both live in the globals of every
        # module the sandbox runs, so a from-import that reached them
        # would hand user code the keys to the sandbox. Reading either as
        # a bare name is refused by the rewriter and as an attribute by
        # the attribute gate; this is the third door. Sandbox-run modules
        # (workspace modules and the `main` proxy) go further and export
        # no dunder at all -- see _module_export.
        if name == "__builtins__" or name.startswith("__st_"):
            raise ImportError(
                f"cannot import name '{name}' from '{module_name}': "
                "sandbox internals are not importable"
            )

        # `from sys import stdin, argv, ...` — mirror the __st_import__
        # synthetic-sys branch (sys is otherwise blocked by policy).
        if _sandbox_sys is not None and module_name == "sys":
            if hasattr(_sandbox_sys, name):
                return getattr(_sandbox_sys, name)
            raise ImportError(f"cannot import name '{name}' from 'sys'")

        exec_mod = exec_modules.get(module_name)
        if exec_mod is not None:
            # Straight out of the instance dict: the exposed set is the
            # mapping the embedder wrote, and `getattr` would answer for
            # the module type's own names on top of it.
            if name in exec_mod._st_exposed:
                return exec_mod.__dict__[name]
            raise ImportError(f"cannot import name '{name}' from '{module_name}'")

        if _level > 0:
            # Relative import — resolve against caller's __file__
            caller_file = sys._getframe(1 + _depth).f_globals.get("__file__", "")
            base_dir = posixpath.dirname(caller_file)
            for _ in range(_level - 1):
                base_dir = posixpath.dirname(base_dir)
            # base_dir is an absolute VFS path, but dotted module names
            # are relative to the module root — strip it before dotting
            # or resolve_module would prepend the root a second time.
            if vfs._root and base_dir.startswith(vfs._root):
                base_dir = base_dir[len(vfs._root) :]

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
                value = _module_export(mod, name)
                if value is not _MISSING:
                    return value
                raise ImportError(f"cannot import name '{name}' from '{abs_module}'")
            raise ImportError(
                f"No module named '{abs_module}' (resolved from relative import)"
            )

        # Try policy first
        if policy.is_import_allowed(module_name):
            value = policy.resolve_module_member(module_name, name)
            # Find effective registration by name to support submodules
            # of recursive registrations (e.g. from os.path import join
            # when only os is registered with recursive=True).
            reg = policy.modules.get(module_name)
            if reg is None:
                parts = module_name.split(".")
                for i in range(len(parts) - 1, 0, -1):
                    parent = ".".join(parts[:i])
                    p_reg = policy.modules.get(parent)
                    if p_reg and p_reg.recursive:
                        reg = p_reg
                        break
            return _maybe_wrap_privileged(value, reg, name)

        # Try VFS modules
        mod = vfs.resolve_module(module_name)
        if mod is not None:
            value = _module_export(mod, name)
            if value is not _MISSING:
                return value
            # name might be a sub-module (from pkg import sub)
            sub = vfs.resolve_module(module_name + "." + name)
            if sub is not None:
                return sub
            raise ImportError(f"cannot import name '{name}' from '{module_name}'")

        # module_name might be a package directory without __init__.py
        sub = vfs.resolve_module(module_name + "." + name)
        if sub is not None:
            return sub

        # "from main import X" / "from __main__ import X" — resolve from
        # the sandbox namespace.  LLMs frequently attempt this pattern to
        # import globals that are already available in the execution scope.
        if module_name in ("main", "__main__"):
            caller_globals = sys._getframe(1 + _depth).f_globals
            if not (name.startswith("__") and name.endswith("__")):
                if name in caller_globals:
                    return caller_globals[name]

        lineno = _caller_lineno(2 + _depth)
        loc = f" (line {lineno})" if lineno else ""
        raise ImportError(_import_error_message(module_name) + loc)

    def _submodule_exists(full_name: str) -> bool:
        """Does *full_name* exist as an importable submodule?

        Existence only -- permission is the caller's business.  This is what
        separates an absent fromlist entry (tolerated, as CPython tolerates
        it) from one that exists but fails to load (must propagate).
        """
        try:
            return importlib.util.find_spec(full_name) is not None
        except (ImportError, AttributeError, ValueError):
            return False

    def _vfs_package(name: str, fromlist: Any) -> Any:
        """Resolve a VFS package *directory* named *name*, if there is one.

        ``ensure_package_chain`` only builds parent packages for a *dotted*
        target, so drive it with a fromlist entry and then walk back down to
        the package the caller actually asked for.  Returns None when no
        entry names a real module; failures raised by a module's own body
        propagate.
        """
        for entry in fromlist:
            if not isinstance(entry, str) or entry == "*":
                continue
            top = vfs.ensure_package_chain(f"{name}.{entry}")
            if top is None:
                continue
            obj = top
            for part in name.split(".")[1:]:
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj is not None:
                return obj
        return None

    def __st_dynimport__(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        """Policy-gated ``__import__`` for sandboxed code.

        The rewriter redirects every source-level read of ``__import__``
        here (see ``Rewriter.visit_Name``), so a computed import name lands
        on the same policy check as an ``import`` statement.  This is safe
        because the two lookups are genuinely separate in CPython: the
        ``import`` statement resolves ``__import__`` from the frame's
        *builtins*, where the real one stays parked for C extensions, while
        a source-level ``__import__`` is an ordinary name load.  Gating the
        latter therefore never touches library internals.

        ``globals``/``locals`` are accepted for signature compatibility and
        ignored -- CPython consults them only to resolve ``level > 0``,
        which this gate declines.
        """
        if level:
            lineno = _caller_lineno()
            loc = f" (line {lineno})" if lineno else ""
            raise ImportError(
                "Relative __import__ (level > 0) is not supported; use a "
                f"'from . import ...' statement instead{loc}"
            )

        if not fromlist:
            # Bare __import__('a.b') binds the top-level package, which is
            # exactly __st_import__'s no-alias branch.
            return __st_import__(name, _depth=1)

        # With a non-empty fromlist CPython returns the module named by
        # `name` itself; passing the name as its own alias selects that
        # branch of __st_import__.
        try:
            mod = __st_import__(name, alias=name, _depth=1)
        except ImportError:
            # A VFS *package directory* doesn't resolve that way: packages
            # are built by ensure_package_chain, not found as <name>.py.
            # `from pkg import sub` already works, so the dynamic spelling
            # of the same import must not be stricter.
            mod = _vfs_package(name, fromlist)
            if mod is None:
                raise

        # CPython also imports each fromlist entry that turns out to be a
        # submodule, binding it on the parent -- do the same so that
        # __import__('PIL', fromlist=['Image']).Image resolves for packages
        # whose __init__ doesn't eager-import its submodules.
        for entry in fromlist:
            if not isinstance(entry, str) or entry == "*" or hasattr(mod, entry):
                continue
            full = f"{name}.{entry}"
            if vfs.resolve_module(full) is not None:
                continue  # VFS submodule; resolving it attached it
            if not policy.is_import_allowed(full) or not _submodule_exists(full):
                # Absent or ungranted.  CPython tolerates absent fromlist
                # entries, and `__import__(m, fromlist=['dummy'])` is a
                # standard idiom for "give me the leaf, not the top
                # package".  A denied name resurfaces on the attribute
                # access that follows, which the policy gates in its own
                # right.
                continue
            # It exists and is granted, so anything raised while loading it
            # is a real failure -- a missing dependency, an error in its
            # body, or a StTimeout/StCancelled from a checkpoint (those are
            # plain Exception subclasses, see errors.StError).  Reducing any
            # of those to a silent success is how the statement form and
            # this one drift apart.  Deliberately uncaught.
            __st_importfrom__(name, entry, _depth=1)
        return mod

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
    _in_exec_box = [True]  # True during sb.exec(), False after
    _callback_depth = [0]  # Tracks nesting depth of callback invocations

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

    def __st_capture_context__(fn: Any) -> Any:
        """Wrap a callable to restore sandbox ContextVars when called outside exec.

        Used in raw mode to ensure that callbacks (NiceGUI on_click, on_change,
        etc.) retain filesystem and network isolation even though they fire in a
        different asyncio Task after sb.exec() has returned.

        Captures the current ``current_fs`` and ``network_allowed`` values at
        decoration time and restores them on every call.  Also resets the
        checkpoint timer and tick counter so each callback gets a fresh budget.
        """
        captured_fs = current_fs.get(None)
        captured_net = network_allowed.get()

        # No restrictions active — skip wrapping entirely.
        if captured_fs is None and captured_net:
            return fn

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _in_exec_box[0] and _callback_depth[0] == 0:
                    _tick_counter[0] = 0
                    _start_time_box[0] = time.monotonic()
                _callback_depth[0] += 1
                tok_fs = (
                    current_fs.set(captured_fs) if captured_fs is not None else None
                )
                tok_net = network_allowed.set(captured_net)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    _callback_depth[0] -= 1
                    network_allowed.reset(tok_net)
                    if tok_fs is not None:
                        current_fs.reset(tok_fs)

            return async_wrapper
        else:

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _in_exec_box[0] and _callback_depth[0] == 0:
                    _tick_counter[0] = 0
                    _start_time_box[0] = time.monotonic()
                _callback_depth[0] += 1
                tok_fs = (
                    current_fs.set(captured_fs) if captured_fs is not None else None
                )
                tok_net = network_allowed.set(captured_net)
                try:
                    return fn(*args, **kwargs)
                finally:
                    _callback_depth[0] -= 1
                    network_allowed.reset(tok_net)
                    if tok_fs is not None:
                        current_fs.reset(tok_fs)

            return wrapper

    gates["__st_tick_counter__"] = _tick_counter
    gates["__st_start_time__"] = _start_time_box
    gates["__st_cancel_flag__"] = _cancel_flag_box
    gates["__st_memory__"] = _memory_box
    gates["__st_in_exec__"] = _in_exec_box
    gates.update(
        {
            "__st_getattr__": __st_getattr__,
            "__st_setattr__": __st_setattr__,
            "__st_delattr__": __st_delattr__,
            "__st_import__": __st_import__,
            "__st_importfrom__": __st_importfrom__,
            "__st_dynimport__": __st_dynimport__,
            "__st_defun__": __st_defun__,
            "__st_defclass__": __st_defclass__,
            "__st_checkpoint__": __st_checkpoint__,
            "__st_capture_context__": __st_capture_context__,
            "__st_vfs__": vfs,
            "__st_exec_modules__": exec_modules,
        }
    )
    return gates
