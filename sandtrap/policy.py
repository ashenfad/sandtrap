import fnmatch
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Iterable, Union

Pattern = Union[str, Iterable[str], Callable[[str], bool]]


def _make_predicate(pattern: Pattern | None) -> Callable[[str], bool]:
    """Convert a Pattern into a predicate function."""
    if pattern is None:
        return lambda _name: False
    if isinstance(pattern, str):
        return lambda name, p=pattern: fnmatch.fnmatch(name, p)
    if callable(pattern):
        return pattern
    # Iterable of patterns
    sub_preds = [_make_predicate(p) for p in pattern]
    return lambda name: any(p(name) for p in sub_preds)


@dataclass
class MemberSpec:
    """Per-member configuration overrides for use in the `configure` dict."""

    host_fs_access: bool = False
    network_access: bool = False


@dataclass
class _FnRegistration:
    func: Callable
    name: str
    host_fs_access: bool = False
    network_access: bool = False


@dataclass
class _ClsRegistration:
    cls: type
    name: str
    constructable: bool = True
    include: Pattern = "*"
    exclude: Pattern = "_*"
    configure: dict[str, MemberSpec] = field(default_factory=dict)
    host_fs_access: bool = False
    network_access: bool = False

    def __post_init__(self) -> None:
        self._include_pred = _make_predicate(self.include)
        self._exclude_pred = _make_predicate(self.exclude)


@dataclass
class _ModuleRegistration:
    obj: Any
    name: str
    include: Pattern = "*"
    exclude: Pattern = field(default_factory=lambda: ("_*", "*._*"))
    configure: dict[str, MemberSpec] = field(default_factory=dict)
    recursive: bool = False
    host_fs_access: bool = False
    network_access: bool = False

    def __post_init__(self) -> None:
        self._include_pred = _make_predicate(self.include)
        self._exclude_pred = _make_predicate(self.exclude)


# Interpreter-internal attributes that don't start with underscore but
# expose frames, code objects, and execution internals.  Blocked by default
# to prevent sandboxed code from reaching the execution namespace (e.g.
# via generator.gi_frame.f_globals) and tampering with gate functions.
BLOCKED_INTERNAL_ATTRS = frozenset(
    {
        # Generator / coroutine / async-generator frame & code access
        "gi_frame",
        "gi_code",
        "gi_yieldfrom",
        "cr_frame",
        "cr_code",
        "cr_origin",
        "ag_frame",
        "ag_code",
        "ag_await",
        # Frame internals
        "f_globals",
        "f_locals",
        "f_builtins",
        "f_code",
        "f_back",
    }
)

# Default dunders accessible in sandboxed code
DEFAULT_ALLOWED_DUNDERS = frozenset(
    {
        "__init__",
        "__str__",
        "__repr__",
        "__len__",
        "__iter__",
        "__next__",
        "__getitem__",
        "__setitem__",
        "__delitem__",
        "__contains__",
        "__eq__",
        "__ne__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
        "__hash__",
        "__bool__",
        "__enter__",
        "__exit__",
        "__aenter__",
        "__aexit__",
        "__aiter__",
        "__anext__",
        "__add__",
        "__radd__",
        "__sub__",
        "__rsub__",
        "__mul__",
        "__rmul__",
        "__truediv__",
        "__rtruediv__",
        "__floordiv__",
        "__rfloordiv__",
        "__mod__",
        "__rmod__",
        "__pow__",
        "__rpow__",
        "__neg__",
        "__pos__",
        "__abs__",
        "__int__",
        "__float__",
        "__index__",
        "__call__",
    }
)


class Policy:
    """Defines what sandboxed code is allowed to access."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        memory_limit: int | None = None,
        max_stdout: int | None = None,
        allow_network: bool = False,
        tick_limit: int | None = None,
    ) -> None:
        self.functions: dict[str, _FnRegistration] = {}
        self.classes: dict[str, _ClsRegistration] = {}
        self.modules: dict[str, _ModuleRegistration] = {}

        # O(1) lookup for _find_registration_for
        self._reg_by_cls_id: dict[int, _ClsRegistration] = {}
        self._reg_by_module_id: dict[int, _ModuleRegistration] = {}

        # Global flags
        self.allow_network = allow_network

        # Resource limits
        self.timeout = timeout
        self.memory_limit = memory_limit  # MB of additional allocation headroom
        self.max_stdout = max_stdout  # max chars of stdout (keeps tail)
        self.tick_limit = tick_limit  # max checkpoint ticks per execution

    def fn(
        self,
        func: Callable | None = None,
        *,
        name: str | None = None,
        host_fs_access: bool = False,
        network_access: bool = False,
    ) -> Callable:
        """Register a function. Usable as @policy.fn or @policy.fn(...)."""

        def _register(f: Callable) -> Callable:
            fn_name = name or f.__name__
            self.functions[fn_name] = _FnRegistration(
                func=f,
                name=fn_name,
                host_fs_access=host_fs_access,
                network_access=network_access,
            )
            return f

        if func is not None:
            return _register(func)
        return _register

    def cls(
        self,
        cls: type | None = None,
        *,
        name: str | None = None,
        constructable: bool = True,
        include: Pattern = "*",
        exclude: Pattern = "_*",
        configure: dict[str, MemberSpec] | None = None,
        host_fs_access: bool = False,
        network_access: bool = False,
    ) -> type | Callable[[type], type]:
        """Register a class. Usable as @policy.cls or @policy.cls(...)."""

        def _register(c: type) -> type:
            cls_name = name or c.__name__
            reg = _ClsRegistration(
                cls=c,
                name=cls_name,
                constructable=constructable,
                include=include,
                exclude=exclude,
                configure=configure or {},
                host_fs_access=host_fs_access,
                network_access=network_access,
            )
            self.classes[cls_name] = reg
            self._reg_by_cls_id[id(c)] = reg
            return c

        if cls is not None:
            return _register(cls)
        return _register

    def module(
        self,
        obj: ModuleType | Any,
        *,
        name: str | None = None,
        include: Pattern = "*",
        exclude: Pattern = ("_*", "*._*"),
        configure: dict[str, MemberSpec] | None = None,
        recursive: bool = False,
        host_fs_access: bool = False,
        network_access: bool = False,
    ) -> None:
        """Register a module or live object instance."""
        if name is None:
            if isinstance(obj, ModuleType):
                mod_name = obj.__name__
            else:
                raise ValueError(
                    "name is required when registering a live object instance"
                )
        else:
            mod_name = name
        reg = _ModuleRegistration(
            obj=obj,
            name=mod_name,
            include=include,
            exclude=exclude,
            configure=configure or {},
            recursive=recursive,
            host_fs_access=host_fs_access,
            network_access=network_access,
        )
        self.modules[mod_name] = reg
        self._reg_by_module_id[id(obj)] = reg

    def needs_network(self) -> bool:
        """Return True if any part of this policy requires network access."""
        if self.allow_network:
            return True
        for reg in self.functions.values():
            if reg.network_access:
                return True
        for reg in self.classes.values():
            if reg.network_access:
                return True
            for spec in reg.configure.values():
                if spec.network_access:
                    return True
        for reg in self.modules.values():
            if reg.network_access:
                return True
            for spec in reg.configure.values():
                if spec.network_access:
                    return True
        return False

    def needs_host_fs(self) -> bool:
        """Return True if any registration requires host filesystem access."""
        for reg in self.functions.values():
            if reg.host_fs_access:
                return True
        for reg in self.classes.values():
            if reg.host_fs_access:
                return True
            for spec in reg.configure.values():
                if spec.host_fs_access:
                    return True
        for reg in self.modules.values():
            if reg.host_fs_access:
                return True
            for spec in reg.configure.values():
                if spec.host_fs_access:
                    return True
        return False

    def is_attr_allowed(self, obj: Any, attr: str) -> bool:
        """Check if an attribute access is permitted by this policy."""
        # Check registered module/class-specific rules first
        reg = self._find_registration_for(obj)
        if reg is not None:
            # Check configure overrides
            if hasattr(reg, "configure") and attr in reg.configure:
                return True  # Explicitly configured members are allowed

            # Check include/exclude
            if hasattr(reg, "_include_pred"):
                if not reg._include_pred(attr):
                    # Before denying, check if the attribute is a separately
                    # registered submodule (e.g., os.path registered alongside os)
                    sub_obj = getattr(obj, attr, None)
                    if (
                        sub_obj is not None
                        and self._find_registration_for(sub_obj) is not None
                    ):
                        return True
                    return False
                if reg._exclude_pred(attr):
                    return False
                # Block submodule access on non-recursive module registrations
                if not getattr(reg, "recursive", False):
                    sub_obj = getattr(obj, attr, None)
                    if isinstance(sub_obj, ModuleType):
                        # Allow if the submodule is separately registered
                        if self._find_registration_for(sub_obj) is None:
                            return False
                return True

        # Interpreter-internal attrs (frames, code objects, etc.)
        if attr in BLOCKED_INTERNAL_ATTRS:
            return False
        # Default dunder check
        if attr.startswith("__") and attr.endswith("__"):
            return attr in DEFAULT_ALLOWED_DUNDERS
        # Single-underscore private attrs blocked by default
        if attr.startswith("_"):
            return False
        return True

    def _find_registration_for(self, obj: Any) -> Any:
        """Find a policy registration that applies to the given object."""
        if obj is None:
            return None

        # Handle super objects — walk the MRO to find a registered class
        if type(obj) is super:
            self_class = getattr(obj, "__self_class__", None)
            this_class = getattr(obj, "__thisclass__", None)
            if self_class is not None:
                mro = self_class.__mro__
                start = 0
                if this_class is not None:
                    try:
                        start = mro.index(this_class) + 1
                    except ValueError:
                        pass
                for cls in mro[start:]:
                    reg = self._reg_by_cls_id.get(id(cls))
                    if reg is not None and cls is reg.cls:
                        return reg
            return None

        # Check if obj is a registered module
        reg = self._reg_by_module_id.get(id(obj))
        if reg is not None and obj is reg.obj:
            return reg

        # Check if type(obj) is a registered class
        obj_type = type(obj)
        reg = self._reg_by_cls_id.get(id(obj_type))
        if reg is not None and obj_type is reg.cls:
            return reg

        # Check if obj itself is a registered class (accessing class attrs)
        if isinstance(obj, type):
            reg = self._reg_by_cls_id.get(id(obj))
            if reg is not None and obj is reg.cls:
                return reg

        return None

    def is_import_allowed(self, module_name: str) -> bool:
        """Check if a module import is permitted by this policy."""
        # Direct match
        if module_name in self.modules:
            return True
        # Check if it's a submodule of a recursive registration
        for reg_name, reg in self.modules.items():
            if reg.recursive and module_name.startswith(reg_name + "."):
                return True
        return False

    def resolve_module(self, module_name: str) -> Any:
        """Resolve a module name to the registered object."""
        # Direct match
        if module_name in self.modules:
            return self.modules[module_name].obj
        # Check parent modules for dotted names
        parts = module_name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            parent = ".".join(parts[:i])
            if parent in self.modules and self.modules[parent].recursive:
                obj = self.modules[parent].obj
                for part in parts[i:]:
                    obj = getattr(obj, part)
                return obj
        raise ImportError(f"Module '{module_name}' not registered")

    def resolve_module_member(self, module_name: str, member_name: str) -> Any:
        """Resolve a member from a registered module, checking filters."""
        if not self.is_import_allowed(module_name):
            raise ImportError(f"Import from '{module_name}' is not allowed")

        # Find the registration
        reg = self.modules.get(module_name)
        if reg is None:
            # Check parent registrations for recursive modules
            parts = module_name.split(".")
            for i in range(len(parts) - 1, 0, -1):
                parent = ".".join(parts[:i])
                if parent in self.modules and self.modules[parent].recursive:
                    reg = self.modules[parent]
                    break
        if reg is None:
            raise ImportError(f"Module '{module_name}' not registered")

        # Check include/exclude filters
        if not reg._include_pred(member_name) or reg._exclude_pred(member_name):
            raise ImportError(f"'{member_name}' is not available from '{module_name}'")

        # Get the member from the actual module object
        module_obj = self.resolve_module(module_name)
        if not hasattr(module_obj, member_name):
            raise ImportError(
                f"Module '{module_name}' has no attribute '{member_name}'"
            )
        return getattr(module_obj, member_name)
