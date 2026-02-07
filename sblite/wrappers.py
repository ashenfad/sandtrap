"""Pickleable wrappers for sandbox-defined functions and classes."""

import ast
import copy
import inspect
from collections.abc import Sequence
from typing import Any


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
        return self._compiled(*args, **kwargs)

    def __repr__(self) -> str:
        status = "active" if self._compiled is not None else "inactive"
        return f"<SbFunction '{self._name}' ({status})>"

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        compiled = state.pop("_compiled", None)

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

        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._compiled = None

    def activate(
        self,
        gates: dict[str, Any],
        *,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        """Recompile from stored AST with the given gate functions.

        Args:
            gates: Gate function dict (from make_gates).
            namespace: Optional namespace for globals the function references.
        """
        from .builtins import SAFE_BUILTINS

        # Build execution namespace
        ns: dict[str, Any] = dict(namespace or {})
        ns.update(gates)
        ns["__builtins__"] = dict(SAFE_BUILTINS)
        ns["__name__"] = "__sblite__"

        # Inject frozen closure values (become globals in recompiled code)
        frozen = getattr(self, "_frozen_closure", None)
        if frozen:
            ns.update(frozen)

        # Wrap function AST in a module for compilation
        func_copy = copy.deepcopy(self._func_ast)
        module = ast.Module(body=[func_copy], type_ignores=[])
        ast.fix_missing_locations(module)

        code = compile(module, f"<sblite:fn:{self._name}>", "exec")
        exec(code, ns)  # noqa: S102

        self._compiled = ns[self._name]


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

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._compiled_cls is None:
            raise RuntimeError(
                f"SbClass '{self._name}' is not active "
                f"-- call activate() first"
            )
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
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._compiled_cls = None
        self._sb_getattr_gate = None

    def activate(
        self,
        gates: dict[str, Any],
        *,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        """Recompile class from stored AST with the given gate functions."""
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

        # Auto-activate any frozen SbFunction/SbClass refs
        for val in ns.values():
            if isinstance(val, SbFunction) and val._compiled is None:
                val.activate(gates, namespace=ns)
            elif isinstance(val, SbClass) and val._compiled_cls is None:
                val.activate(gates, namespace=ns)

        class_copy = copy.deepcopy(self._class_ast)
        module = ast.Module(body=[class_copy], type_ignores=[])
        ast.fix_missing_locations(module)

        code = compile(module, f"<sblite:cls:{self._name}>", "exec")
        exec(code, ns)  # noqa: S102

        self._compiled_cls = ns[self._name]


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
