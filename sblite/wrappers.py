"""Pickleable wrappers for sandbox-defined functions and classes."""

import ast
import copy
import inspect
from collections.abc import Sequence
from typing import Any

from .builtins import _SAFE_EXCEPTIONS, _SAFE_FN_NAMES

_SAFE_BUILTIN_NAMES = set(_SAFE_FN_NAMES) | set(_SAFE_EXCEPTIONS) | {
    "True", "False", "None", "Ellipsis", "NotImplemented",
    "print", "getattr", "hasattr", "locals",
}


def _collect_global_names(code: Any) -> set[str]:
    """Collect co_names from a code object and all nested code objects.

    Generator expressions, comprehensions, and nested functions create
    separate code objects.  Global names used inside them appear in the
    nested code's co_names, not the parent's.
    """
    names: set[str] = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            names.update(_collect_global_names(const))
    return names


def _extract_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Extract all non-internal Name.id references from AST nodes."""
    names: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and not child.id.startswith("__sb_"):
                names.add(child.id)
    return names


class SbFunction:
    """Callable, pickleable wrapper for a sandbox-defined function.

    Stores the rewritten AST so the function can be serialized and
    recompiled after deserialization.
    """

    def __init__(
        self,
        name: str,
        compiled_fn: Any,
        func_ast: ast.FunctionDef,
    ) -> None:
        self._name = name
        self._compiled = compiled_fn
        self._func_ast = func_ast
        self._sandbox: Any = None  # set by activate() or auto-activation
        self._gates: dict[str, Any] | None = None

        # Copy plain-data metadata from compiled function
        self.__name__ = name
        if compiled_fn is not None:
            self.__qualname__ = getattr(compiled_fn, "__qualname__", name)
            self.__doc__ = getattr(compiled_fn, "__doc__", None)
            self.__annotations__ = getattr(compiled_fn, "__annotations__", {})
            self.__defaults__ = getattr(compiled_fn, "__defaults__", None)
            self.__kwdefaults__ = getattr(compiled_fn, "__kwdefaults__", None)
            try:
                self.__signature__ = inspect.signature(compiled_fn)
            except (ValueError, TypeError):
                self.__signature__ = None
        else:
            self.__qualname__ = name
            self.__doc__ = None
            self.__annotations__ = {}
            self.__defaults__ = None
            self.__kwdefaults__ = None
            self.__signature__ = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._compiled is None:
            raise RuntimeError(
                f"SbFunction '{self._name}' is not active "
                f"-- call activate() with gate functions first"
            )
        if self._sandbox is not None and self._gates is not None:
            return self._sandbox._call_in_context(
                self._compiled, self._gates, args, kwargs
            )
        return self._compiled(*args, **kwargs)

    def __repr__(self) -> str:
        status = "active" if self._compiled is not None else "inactive"
        return f"<SbFunction '{self._name}' ({status})>"

    @property
    def global_refs(self) -> set[str]:
        """Names this function references as globals (excluding builtins/gates)."""
        stored = getattr(self, "_global_ref_names", None)
        if stored is not None:
            return set(stored)
        if self._compiled is not None:
            return {
                n for n in _collect_global_names(self._compiled.__code__)
                if not n.startswith("__sb_")
                and n not in _SAFE_BUILTIN_NAMES
                and n != self._name
            }
        return set()

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        compiled = state.pop("_compiled", None)
        state.pop("_sandbox", None)
        state.pop("_gates", None)

        # Freeze closure variables from the compiled function
        if compiled is not None and compiled.__closure__:
            freevars = compiled.__code__.co_freevars
            frozen: dict[str, Any] = {}
            for name, cell in zip(freevars, compiled.__closure__):
                if name.startswith("__sb_"):
                    continue  # Gates are re-injected on activate
                try:
                    frozen[name] = cell.cell_contents
                except ValueError:
                    continue  # Empty cell
            if frozen:
                state["_frozen_closure"] = frozen

        # Freeze global references (SbFunction/SbClass only) and store
        # all global ref names for introspection via global_refs property
        if compiled is not None:
            globs = compiled.__globals__
            frozen_globals: dict[str, Any] = {}
            global_ref_names: set[str] = set()
            for name in _collect_global_names(compiled.__code__):
                if name.startswith("__sb_"):
                    continue
                if name in _SAFE_BUILTIN_NAMES:
                    continue
                if name == self._name:
                    continue
                global_ref_names.add(name)
                if name in globs and isinstance(globs[name], (SbFunction, SbClass)):
                    frozen_globals[name] = globs[name]
            if frozen_globals:
                state["_frozen_globals"] = frozen_globals
            if global_ref_names:
                state["_global_ref_names"] = global_ref_names

        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._compiled = None
        self._sandbox = None
        self._gates = None

    def activate(
        self,
        gates: dict[str, Any],
        *,
        sandbox: Any = None,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        """Recompile from stored AST with the given gate functions.

        Args:
            gates: Gate function dict (from make_gates).
            sandbox: Optional Sandbox reference for direct-call context.
            namespace: Optional namespace for globals the function references.

        Namespace priority (lowest to highest):
            1. Frozen globals (fallback from pickle)
            2. Caller namespace (late-binding override)
            3. Frozen closure (value captures, sacred)
            4. Gates + builtins (always on top)
        """
        from .builtins import SAFE_BUILTINS

        ns: dict[str, Any] = {}

        frozen_globals = getattr(self, "_frozen_globals", None)
        if frozen_globals:
            ns.update(frozen_globals)

        if namespace:
            ns.update(namespace)

        frozen_closure = getattr(self, "_frozen_closure", None)
        if frozen_closure:
            ns.update(frozen_closure)

        ns.update(gates)
        ns["__builtins__"] = dict(SAFE_BUILTINS)
        ns["__name__"] = "__sblite__"

        # Auto-activate frozen globals
        if frozen_globals:
            for val in frozen_globals.values():
                if isinstance(val, SbFunction) and val._compiled is None:
                    val.activate(gates, sandbox=sandbox, namespace=ns)
                elif isinstance(val, SbClass) and val._compiled_cls is None:
                    val.activate(gates, sandbox=sandbox, namespace=ns)

        # Wrap function AST in a module for compilation
        func_copy = copy.deepcopy(self._func_ast)
        module = ast.Module(body=[func_copy], type_ignores=[])
        ast.fix_missing_locations(module)

        code = compile(module, f"<sblite:fn:{self._name}>", "exec")
        exec(code, ns)  # noqa: S102

        self._compiled = ns[self._name]
        self._sandbox = sandbox
        self._gates = gates


class SbClass:
    """Pickleable wrapper for a sandbox-defined class.

    Stores the rewritten AST and frozen references to decorators/bases
    so the class can be serialized and recompiled after deserialization.
    """

    def __init__(
        self,
        name: str,
        compiled_cls: type,
        class_ast: ast.ClassDef,
        frozen_refs: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._compiled_cls = compiled_cls
        self._class_ast = class_ast
        self._frozen_refs = frozen_refs or {}
        self._sb_getattr_gate: Any = None
        self._sandbox: Any = None
        self._gates: dict[str, Any] | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._compiled_cls is None:
            raise RuntimeError(
                f"SbClass '{self._name}' is not active "
                f"-- call activate() first"
            )
        if self._sandbox is not None and self._gates is not None:
            instance = self._sandbox._call_in_context(
                self._compiled_cls, self._gates, args, kwargs
            )
        else:
            instance = self._compiled_cls(*args, **kwargs)
        return SbInstance(self, instance, self._sb_getattr_gate)

    def __mro_entries__(self, bases: tuple) -> tuple:
        """Allow SbClass to be used as a base class in class statements."""
        return (self._compiled_cls,)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        compiled = self.__dict__.get("_compiled_cls")
        if compiled is None:
            raise RuntimeError(
                f"SbClass '{self._name}' is not active "
                f"-- call activate() first"
            )
        return getattr(compiled, name)

    def __repr__(self) -> str:
        status = "active" if self._compiled_cls is not None else "inactive"
        return f"<SbClass '{self._name}' ({status})>"

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state.pop("_compiled_cls", None)
        state.pop("_sb_getattr_gate", None)
        state.pop("_sandbox", None)
        state.pop("_gates", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._compiled_cls = None
        self._sb_getattr_gate = None
        self._sandbox = None
        self._gates = None

    def activate(
        self,
        gates: dict[str, Any],
        *,
        sandbox: Any = None,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        """Recompile class from stored AST with the given gate functions."""
        if getattr(self, "_activating", False):
            return  # Guard against circular activation (e.g. mutual base refs)
        self._activating = True
        try:
            self._activate_inner(gates, sandbox=sandbox, namespace=namespace)
        finally:
            self._activating = False

    def _activate_inner(
        self,
        gates: dict[str, Any],
        *,
        sandbox: Any = None,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        from .builtins import SAFE_BUILTINS

        # Frozen refs first (lowest priority), then caller namespace overrides
        ns: dict[str, Any] = {}
        if self._frozen_refs:
            ns.update(self._frozen_refs)
        if namespace:
            ns.update(namespace)
        ns.update(gates)
        ns["__builtins__"] = dict(SAFE_BUILTINS)
        ns["__name__"] = "__sblite__"

        # Auto-activate frozen refs only (not the full namespace — that's
        # the sandbox's job via _auto_activate to avoid ordering issues)
        if self._frozen_refs:
            for val in self._frozen_refs.values():
                if isinstance(val, SbFunction) and val._compiled is None:
                    val.activate(gates, sandbox=sandbox, namespace=ns)
                elif isinstance(val, SbClass) and val._compiled_cls is None:
                    val.activate(gates, sandbox=sandbox, namespace=ns)

        class_copy = copy.deepcopy(self._class_ast)
        module = ast.Module(body=[class_copy], type_ignores=[])
        ast.fix_missing_locations(module)

        code = compile(module, f"<sblite:cls:{self._name}>", "exec")
        exec(code, ns)  # noqa: S102

        self._compiled_cls = ns[self._name]
        self._sandbox = sandbox
        self._gates = gates


class SbInstance:
    """Pickleable wrapper for an instance of a sandbox-defined class.

    Proxies attribute access to the underlying real instance and stores
    the SbClass reference + instance __dict__ for serialization.
    """

    __slots__ = ("_sb_class", "_sb_instance", "_frozen_attrs", "_sb_getattr_gate")

    def __init__(self, sb_class: SbClass, instance: Any, getattr_gate: Any = None) -> None:
        object.__setattr__(self, "_sb_class", sb_class)
        object.__setattr__(self, "_sb_instance", instance)
        object.__setattr__(self, "_sb_getattr_gate", getattr_gate)

    def __getattr__(self, name: str) -> Any:
        instance = object.__getattribute__(self, "_sb_instance")
        if instance is None:
            raise RuntimeError(
                "SbInstance is not active -- call activate() first"
            )
        gate = object.__getattribute__(self, "_sb_getattr_gate")
        if gate is not None:
            return gate(instance, name)
        return getattr(instance, name)

    def __setattr__(self, name: str, value: Any) -> None:
        instance = object.__getattribute__(self, "_sb_instance")
        if instance is None:
            raise RuntimeError(
                "SbInstance is not active -- call activate() first"
            )
        setattr(instance, name, value)

    def __delattr__(self, name: str) -> None:
        instance = object.__getattribute__(self, "_sb_instance")
        if instance is None:
            raise RuntimeError(
                "SbInstance is not active -- call activate() first"
            )
        delattr(instance, name)

    def __repr__(self) -> str:
        try:
            instance = object.__getattribute__(self, "_sb_instance")
            if instance is not None:
                return repr(instance)
        except AttributeError:
            pass
        sb_class = object.__getattribute__(self, "_sb_class")
        return f"<SbInstance of '{sb_class._name}' (inactive)>"

    def __getstate__(self) -> dict[str, Any]:
        instance = object.__getattribute__(self, "_sb_instance")
        attrs = instance.__dict__.copy() if instance is not None else {}
        return {
            "sb_class": object.__getattribute__(self, "_sb_class"),
            "attrs": attrs,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        object.__setattr__(self, "_sb_class", state["sb_class"])
        object.__setattr__(self, "_sb_instance", None)
        object.__setattr__(self, "_frozen_attrs", state["attrs"])
        object.__setattr__(self, "_sb_getattr_gate", None)

    def activate(self) -> None:
        """Restore the real instance from frozen attrs.

        The SbClass must be activated before calling this.
        """
        sb_class = object.__getattribute__(self, "_sb_class")
        if sb_class._compiled_cls is None:
            raise RuntimeError(
                "SbClass must be activated before its instances"
            )
        cls = sb_class._compiled_cls
        instance = cls.__new__(cls)
        frozen = object.__getattribute__(self, "_frozen_attrs")
        instance.__dict__.update(frozen)
        object.__setattr__(self, "_sb_instance", instance)


def _make_dunder_forwarder(name: str):
    """Create a forwarding method for a dunder on SbInstance.

    These go directly to the underlying instance, bypassing the attr gate.
    Protocol dunders (__len__, __iter__, __add__, etc.) are safe — they only
    operate on the object's own data and don't expose interpreter internals.
    """
    def forwarder(self, *args, **kwargs):
        instance = object.__getattribute__(self, "_sb_instance")
        return getattr(instance, name)(*args, **kwargs)
    forwarder.__name__ = name
    forwarder.__qualname__ = f"SbInstance.{name}"
    return forwarder


# Dunders that need forwarding for implicit protocol dispatch (str(), len(), etc.).
# __repr__ is handled explicitly above; __init__/__getattr__/__setattr__/__delattr__
# are part of SbInstance's own proxy machinery.
_FORWARDED_DUNDERS = [
    "__str__", "__len__", "__bool__", "__hash__",
    "__iter__", "__next__", "__contains__",
    "__getitem__", "__setitem__", "__delitem__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
    "__add__", "__radd__", "__sub__", "__rsub__",
    "__mul__", "__rmul__", "__truediv__", "__rtruediv__",
    "__floordiv__", "__rfloordiv__", "__mod__", "__rmod__",
    "__pow__", "__rpow__", "__neg__", "__pos__", "__abs__",
    "__int__", "__float__", "__index__",
    "__enter__", "__exit__",
    "__aenter__", "__aexit__", "__aiter__", "__anext__",
    "__call__",
]

for _dname in _FORWARDED_DUNDERS:
    setattr(SbInstance, _dname, _make_dunder_forwarder(_dname))
