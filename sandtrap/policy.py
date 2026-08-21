import fnmatch
import functools
import importlib
import importlib.util
import inspect
import pickle
import warnings
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Iterable, Union

Pattern = Union[str, Iterable[str], Callable[[str], bool]]

# Predicates compiled from include/exclude by __post_init__. Derived state: they
# are local closures (unpicklable by construction) built from patterns that are
# already fields, so they are rebuilt on load rather than sent. A worker that
# isn't forked from this process receives its policy as pickle -- see
# docs/forkserver-design.md.
_DERIVED_PREDICATES = (
    "_include_pred",
    "_exclude_pred",
    "_include_qual_pred",
    "_exclude_qual_pred",
)


@dataclass(frozen=True)
class _ModuleByName:
    """A module grant in transit.

    Module objects don't pickle; their names do. Re-importing in the receiving
    process is what lets a policy reach a worker that didn't inherit this
    process's memory -- and it hands that worker a *fresh* module, with fresh
    C-library state, which is the point of not forking in the first place.
    """

    name: str


_LIVE_OBJECT_MIGRATION = """Register the CLASS and bind the instance instead:

    policy.cls(Client, include=("make_query",))
    sandbox.exec(code, namespace={"c": client})

That applies the same member filters and per-member privileges (``configure``),
and keeps the policy picklable, so it can reach a worker that is not forked from
this process.

Under isolation="process"/"kernel" the current form already hands the worker a
COPY -- mutations sandboxed code makes never reach the object here -- so this
closes a silent bug rather than removing a working feature."""


def _warn_live_object(
    call: str, name: str, detail: str, *, import_note: str = "", stacklevel: int = 2
) -> None:
    warnings.warn(
        f"{call} registered {detail} as {name!r}. This is deprecated and will "
        f"be removed in 0.4.0.\n\n{_LIVE_OBJECT_MIGRATION}{import_note}",
        DeprecationWarning,
        stacklevel=stacklevel,
    )


@dataclass(frozen=True)
class PolicyProblem:
    """One reason a policy can't reach a non-forked worker."""

    kind: str
    """Short classification, e.g. ``"live-object grant"``."""
    name: str
    """The registration it applies to."""
    detail: str
    """What is wrong."""
    remedy: str
    """What to do instead."""

    def __str__(self) -> str:
        return f"{self.name!r} ({self.kind}): {self.detail} {self.remedy}"


def _is_importable(name: str) -> bool:
    """Whether ``name`` could be imported by a *different* interpreter.

    Not the same question as "is it in ``sys.modules`` here". A module built
    at runtime pickles happily by name and then fails to load on the other
    side, which is exactly the case a pre-flight check exists to catch.

    ``find_spec`` raises rather than returning None for a synthetic module
    that was registered in ``sys.modules`` without a ``__spec__`` — a pattern
    embedders do use — so both outcomes mean the same thing here.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError, TypeError):
        return False


def _filter_problems(name: str, reg: Any) -> list[PolicyProblem]:
    """Callable ``include``/``exclude`` predicates can't be serialized."""
    problems = []
    for field_name in ("include", "exclude"):
        value = getattr(reg, field_name, None)
        if callable(value):
            problems.append(
                PolicyProblem(
                    "callable filter",
                    name,
                    f"{field_name}= is a callable predicate, which crosses only "
                    "if it is importable by name.",
                    "Use glob patterns, or move the predicate to a module-level "
                    "function.",
                )
            )
    return problems


def _residual_problem(name: str, reg: Any) -> list[PolicyProblem]:
    """Anything the named checks didn't classify — a locally defined class, an
    odd ``configure`` value. Reported with pickle's own message, since we have
    nothing better to say about it than what it says."""
    try:
        pickle.dumps(reg)
    except Exception as exc:
        return [
            PolicyProblem(
                "unpicklable",
                name,
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}",
                "Register something importable by name (a module-level class "
                "or function).",
            )
        ]
    return []


def _carries_host_state(func: Any) -> str | None:
    """Describe why ``func`` would cross to a worker *by value*, or None.

    Functions and classes pickle by reference: the worker looks them up by
    module and qualname and gets the same object. A callable that carries an
    instance does not -- it pickles the instance too, so the worker calls a
    COPY and the host never sees the effect. That is the same silent
    divergence live-object module grants have, arriving through a different
    door, and it is worse than a refusal because it looks like it worked.

    Unimportable functions (lambdas, closures) are deliberately not named
    here: they fail on their own during pickling, with an error that points
    at the actual lambda.
    """
    seen = 0
    while isinstance(func, functools.partial) and seen < 16:
        # A partial is only as bridgeable as what it wraps AND what it binds:
        # pickling one pickles its arguments, so partial(record, service) copies
        # `service` into the worker even though `record` itself crosses by name.
        for bound in (*func.args, *func.keywords.values()):
            detail = _carries_host_state(bound)
            if detail is not None:
                return f"a partial binding {detail}"
        func = func.func
        seen += 1

    if inspect.ismethod(func):
        owner = func.__self__
        # A classmethod binds to the class, which crosses by name like any
        # other class. Binding to an instance is the problem.
        if not isinstance(owner, type):
            return f"a method bound to a live {type(owner).__name__} instance"
        return None

    if inspect.isfunction(func) or inspect.isbuiltin(func) or inspect.isclass(func):
        return None

    if callable(func):
        return f"a callable {type(func).__name__} instance"

    return None


class _ReconstructsPredicates:
    """Pickle support for registrations that compile include/exclude filters."""

    def __getstate__(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in _DERIVED_PREDICATES}

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self.__post_init__()  # type: ignore[attr-defined]


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


def _dotted_only(pattern: Pattern | None) -> list[str]:
    """The string patterns containing a dot — the only ones that can
    match owner-qualified names. Bare patterns (and callables) apply to
    bare member names only; without this split, ``"_*"`` would match a
    qualified candidate like ``"_ParseResultBase.path"`` and block
    every attribute of classes with private-named bases."""
    if pattern is None or callable(pattern):
        return []
    if isinstance(pattern, str):
        return [pattern] if "." in pattern else []
    return [p for p in pattern if isinstance(p, str) and "." in p]


def _qualified_names(obj: Any, attr: str) -> list[str]:
    """Owner-qualified forms of an attribute access, so dotted patterns
    match: ``"DataFrame.eval"`` (class-qualified, checked against every
    class in the MRO), ``"numpy.random.seed"`` / ``"pandas.core*"``
    (module-path-qualified). Bare patterns keep matching the bare
    attribute name; these are additional candidates checked only
    against dotted patterns."""
    if isinstance(obj, ModuleType):
        name = getattr(obj, "__name__", None)
        return [f"{name}.{attr}"] if name else []
    klass = obj if isinstance(obj, type) else type(obj)
    mro = getattr(klass, "__mro__", (klass,))
    return [f"{c.__name__}.{attr}" for c in mro if c is not object]


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

    def __getstate__(self) -> dict:
        detail = _carries_host_state(self.func)
        if detail is None:
            return self.__dict__.copy()
        raise pickle.PicklingError(
            f"function grant {self.name!r} registers {detail}, which cannot "
            "cross to a worker that is not forked from this process.\n"
            "\n"
            "It would pickle -- by copying the instance -- and the worker "
            "would then call the copy, so anything it records or mutates "
            "would never reach the object here. Refusing beats a grant that "
            "looks like it works.\n"
            "\n"
            "To expose a genuinely live object, register an rpc handler and "
            "pass an RpcProxyMarker in the exec namespace: the object stays "
            "in this process and method calls cross to it. A module-level "
            "function crosses by name and needs nothing special."
        )


@dataclass
class _ClsRegistration(_ReconstructsPredicates):
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
        self._include_qual_pred = _make_predicate(_dotted_only(self.include))
        self._exclude_qual_pred = _make_predicate(_dotted_only(self.exclude))


@dataclass
class _ModuleRegistration(_ReconstructsPredicates):
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
        self._include_qual_pred = _make_predicate(_dotted_only(self.include))
        self._exclude_qual_pred = _make_predicate(_dotted_only(self.exclude))

    def __getstate__(self) -> dict:
        state = super().__getstate__()
        obj = state["obj"]
        if isinstance(obj, ModuleType):
            state["obj"] = _ModuleByName(obj.__name__)
            return state
        raise pickle.PicklingError(
            f"module grant {self.name!r} registers a live object "
            f"({type(obj).__name__}), which cannot cross to a worker that is "
            "not forked from this process.\n"
            "\n"
            "Note this grant is already not what it looks like under "
            'isolation="process": fork hands the worker a copy-on-write '
            "SNAPSHOT, so mutations sandboxed code makes to it never reach "
            "the object in this process.\n"
            "\n"
            "To share a genuinely live object, register an rpc handler and "
            "pass an RpcProxyMarker in the exec namespace -- the object stays "
            "here and method calls cross to it. Note the difference in shape: "
            "attribute reads do not cross, only method calls."
        )

    def __setstate__(self, state: dict) -> None:
        marker = state.get("obj")
        if isinstance(marker, _ModuleByName):
            try:
                state["obj"] = importlib.import_module(marker.name)
            except Exception as exc:
                raise ImportError(
                    f"policy module grant {marker.name!r} could not be "
                    f"re-imported in this process: {exc}. A grant crosses to a "
                    "worker by name, so the module has to be importable there "
                    "-- a dynamically created module cannot be granted to a "
                    "worker that is not forked from its creator."
                ) from exc
        super().__setstate__(state)


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
        module_root: str = "/",
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

        # Where VFS module imports resolve from. `import mod` looks for
        # <module_root>/mod.py — hosts that present the workspace under
        # a prefix (e.g. /workspace) point this at it so imports match
        # what the sandboxed code sees on disk. Absolute, no trailing
        # slash (except "/" itself).
        self.module_root = module_root

    def check_picklable(self) -> list[PolicyProblem]:
        """Every reason this policy can't reach a non-forked worker.

        Returns a list rather than raising, and reports *all* problems rather
        than the first — ``pickle`` gives you one failure at a time, from deep
        inside the serializer, with no idea which registration it came from.
        Discovering that at worker-start time, under load, is how this class of
        bug reaches production.

        Empty list means the policy serializes. Two things it cannot decide:

        * A class defined in ``__main__`` crosses by reference and resolves
          only if the child's ``__main__`` re-imports cleanly — see the
          import-safety note in ``docs/process.md``.
        * Whether a value that crosses *by value* should have. A
          ``functools.partial`` binding a plain instance pickles a copy of it,
          so mutations in the worker never reach this process — but that is
          indistinguishable from binding ordinary configuration data, and
          guessing would reject portable policies. Callables carrying host
          state are caught, because those have no legitimate by-value reading.
        """
        problems: list[PolicyProblem] = []

        for name, reg in self.modules.items():
            obj = reg.obj
            if not isinstance(obj, ModuleType):
                problems.append(
                    PolicyProblem(
                        "live-object grant",
                        name,
                        f"registers a live {type(obj).__name__} instance, which "
                        "cannot be serialized to a worker.",
                        "Register the class and bind the instance in the exec "
                        "namespace instead.",
                    )
                )
                continue
            real_name = getattr(obj, "__name__", name)
            if not _is_importable(real_name):
                problems.append(
                    PolicyProblem(
                        "unimportable module",
                        name,
                        f"module {real_name!r} is not importable outside this "
                        "process, so the grant would pickle here and fail to "
                        "load in the worker.",
                        "Grant an importable module, or expose the same surface "
                        "through a class registration.",
                    )
                )
                continue
            # The residual check only runs when nothing named applies: a
            # callable filter would also fail to pickle, and reporting the same
            # cause twice makes a list of problems harder to act on, not easier.
            named = _filter_problems(name, reg)
            problems.extend(named or _residual_problem(name, reg))

        for name, reg in self.classes.items():
            # The residual check only runs when nothing named applies: a
            # callable filter would also fail to pickle, and reporting the same
            # cause twice makes a list of problems harder to act on, not easier.
            named = _filter_problems(name, reg)
            problems.extend(named or _residual_problem(name, reg))

        for name, reg in self.functions.items():
            detail = _carries_host_state(reg.func)
            if detail is not None:
                problems.append(
                    PolicyProblem(
                        "live callable",
                        name,
                        f"registers {detail}; it would cross as a copy, so the "
                        "worker's calls would never reach this process.",
                        "Register the class and bind the instance in the exec "
                        "namespace instead.",
                    )
                )
                continue
            problems.extend(_residual_problem(name, reg))

        return problems

    def __getstate__(self) -> dict:
        """Drop the ``id()``-keyed indexes; :meth:`__setstate__` rebuilds them.

        These are the one part of a ``Policy`` that would pickle *successfully*
        and arrive wrong. ``id()`` values are addresses in the sending process,
        so a map carried across misses every lookup in the receiving one --
        ``_find_registration_for`` returns None where it should return a
        registration, and the policy quietly decides differently instead of
        failing. Rebuilding is not an optimization here; it is the correctness.
        """
        state = self.__dict__.copy()
        state.pop("_reg_by_cls_id", None)
        state.pop("_reg_by_module_id", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        # Registrations are fully reconstructed before this runs -- module
        # grants re-imported included -- so id() reads the objects this process
        # will actually be asked about.
        self._reg_by_cls_id = {id(reg.cls): reg for reg in self.classes.values()}
        self._reg_by_module_id = {id(reg.obj): reg for reg in self.modules.values()}

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
            # A callable carrying an instance is a live-object grant wearing a
            # different hat: it crosses to a worker as a copy, so the host
            # never sees what it does. Plain functions are unaffected -- they
            # cross by name -- so the decorator forms never warn.
            detail = _carries_host_state(f)
            if detail is not None:
                _warn_live_object("policy.fn()", fn_name, detail, stacklevel=3)
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
        if not isinstance(obj, ModuleType):
            _warn_live_object(
                "policy.module()",
                mod_name,
                f"a live {type(obj).__name__} instance",
                import_note=(
                    f"\n\nNote also that ``import {mod_name}`` stops working for a "
                    "live object -- bind the name in the exec namespace instead."
                ),
                stacklevel=2,
            )
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

            # Check include/exclude — against the bare attribute name and
            # its owner-qualified forms (dotted patterns).
            if hasattr(reg, "_include_pred"):
                quals = _qualified_names(obj, attr)
                if not (
                    reg._include_pred(attr)
                    or any(reg._include_qual_pred(q) for q in quals)
                ):
                    # Before denying, check if the attribute is a separately
                    # registered submodule (e.g., os.path registered alongside os)
                    sub_obj = getattr(obj, attr, None)
                    if (
                        sub_obj is not None
                        and self._find_registration_for(sub_obj) is not None
                    ):
                        return True
                    return False
                if reg._exclude_pred(attr) or any(
                    reg._exclude_qual_pred(q) for q in quals
                ):
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

        # Submodules of a recursive registration inherit the parent's
        # registration (filters included). Without this, members reached
        # via attribute traversal (numpy.random.seed) escaped the very
        # include/exclude that the from-import path already enforced.
        # Walk parents most-specific-first so nested registrations
        # resolve deterministically; a non-recursive parent ends the
        # walk (its submodules inherit nothing — and stay unreachable:
        # traversal and import are both gated on this returning None).
        if isinstance(obj, ModuleType):
            mod_name = getattr(obj, "__name__", "")
            parts = mod_name.split(".")
            for i in range(len(parts) - 1, 0, -1):
                mreg = self.modules.get(".".join(parts[:i]))
                if mreg is not None:
                    return mreg if mreg.recursive else None

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

        # Check if type(obj)'s module is covered by a recursive module registration
        obj_module = getattr(obj_type, "__module__", None)
        if obj_module:
            for reg in self.modules.values():
                if reg.recursive and (
                    obj_module == reg.name or obj_module.startswith(reg.name + ".")
                ):
                    return reg

        return None

    def is_import_allowed(self, module_name: str) -> bool:
        """Check if a module import is permitted by this policy."""
        # Direct match
        if module_name in self.modules:
            return True
        # Check if it's a submodule of a recursive registration —
        # nearest registered parent decides (most-specific-first, so
        # nested registrations resolve deterministically; a
        # non-recursive parent denies). The parent's excludes apply:
        # dotted patterns against the full path
        # (exclude=("pandas.core*",) blocks `import pandas.core.frame`),
        # bare patterns against the terminal segment (the default "_*"
        # blocks `import numpy._core`) — matching what attribute
        # traversal already enforces.
        parts = module_name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            reg = self.modules.get(".".join(parts[:i]))
            if reg is not None:
                if not reg.recursive:
                    return False
                if reg._exclude_qual_pred(module_name):
                    return False
                if reg._exclude_pred(module_name.rsplit(".", 1)[-1]):
                    return False
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
                path = parent
                for part in parts[i:]:
                    path = f"{path}.{part}"
                    try:
                        obj = getattr(obj, part)
                    except AttributeError:
                        # Lazy submodule not yet loaded as a parent attribute.
                        obj = importlib.import_module(path)
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

        # Check include/exclude filters — bare member name plus the
        # module-qualified form, so dotted patterns work here too.
        qual = f"{module_name}.{member_name}"
        if not (reg._include_pred(member_name) or reg._include_qual_pred(qual)) or (
            reg._exclude_pred(member_name) or reg._exclude_qual_pred(qual)
        ):
            raise ImportError(f"'{member_name}' is not available from '{module_name}'")

        # Resolve the member from the actual module object.  Prefer
        # the eager attribute, fall back to importlib for submodules
        # that aren't yet bound on the parent (PIL-style packages
        # whose __init__ doesn't eager-import their submodules).
        module_obj = self.resolve_module(module_name)
        if hasattr(module_obj, member_name):
            candidate = getattr(module_obj, member_name)
        else:
            try:
                candidate = importlib.import_module(f"{module_name}.{member_name}")
            except ImportError:
                raise ImportError(
                    f"Module '{module_name}' has no attribute '{member_name}'"
                ) from None

        # Submodule access bypasses the include/exclude filter (which
        # is for top-level members), so gate it through the same
        # ``is_import_allowed`` check that ``import X.Y`` would hit.
        # Without this, agents could import any submodule the parent
        # happens to expose — eagerly (``os.path`` is bound at import
        # time) or lazily (``PIL.ImageDraw`` via importlib) — even
        # when the parent registration is ``recursive=False``.
        if isinstance(candidate, ModuleType):
            full_name = f"{module_name}.{member_name}"
            if not self.is_import_allowed(full_name):
                raise ImportError(
                    f"Submodule '{full_name}' is not allowed "
                    f"(register '{module_name}' with recursive=True or "
                    f"add '{full_name}' explicitly)."
                )

        return candidate
