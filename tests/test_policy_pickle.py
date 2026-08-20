"""``Policy`` must survive a round trip through pickle.

Prerequisite for workers that aren't forked from the embedding process
(``docs/forkserver-design.md``): spawn and forkserver both send the worker's
configuration as pickle, where fork handed it over by inheriting memory.

Three obstacles, each needing different treatment:

- **Derived predicates.** ``__post_init__`` compiles ``include``/``exclude``
  into local lambdas. Pure derived state -- reconstruct, never serialize.
- **Module objects.** Don't pickle at all; they cross by name and re-import.
- **``id()``-keyed indexes.** These are the dangerous ones. Unlike the others
  they pickle *without complaint* and arrive mapping addresses that mean
  nothing in the new process, so ``_find_registration_for`` silently misses
  and the policy quietly decides differently. Most assertions here exist for
  that failure, which no amount of "does it pickle" testing would catch.
"""

import math
import pickle
import string
import urllib.parse

import pytest

from sandtrap import Policy
from sandtrap.policy import _ClsRegistration, _ModuleRegistration


def roundtrip(policy: Policy) -> Policy:
    return pickle.loads(pickle.dumps(policy))


class Point:
    """Module-level so it pickles by reference, like a real registration."""

    def __init__(self, x: int = 0) -> None:
        self.x = x

    def scale(self, k: int) -> int:
        return self.x * k

    def _private(self) -> None:  # excluded by the default "_*"
        pass


def build_policy() -> Policy:
    """A registration set with the shapes that actually differ: recursive and
    not, dotted and bare patterns, a class, a function."""
    p = Policy(timeout=7.5, tick_limit=1234, module_root="/workspace")
    p.module(math)
    p.module(string, include=("capwords", "Template"))
    p.module(urllib.parse, name="urllib.parse", recursive=True, exclude=("_*", "*._*"))
    p.cls(Point, include="*", exclude="_*")
    p.fn(len, name="length")
    return p


# -- the derived predicates ---------------------------------------------------

NAMES = [
    "capwords",
    "Template",
    "_private",
    "scale",
    "urlparse",
    "Point.scale",
    "Point._private",
    "urllib.parse.urlparse",
    "_ParseResultBase.path",
]


def test_registrations_pickle_at_all():
    """The predicates are local lambdas built in __post_init__; without
    dropping them from the state, a registration of plain strings can't
    pickle."""
    pickle.loads(pickle.dumps(_ModuleRegistration(obj=math, name="math")))
    pickle.loads(pickle.dumps(_ClsRegistration(cls=Point, name="Point")))


def test_rebuilt_predicates_decide_identically():
    """Existing is not enough -- they have to agree, or the policy silently
    changes shape."""
    before = build_policy()
    after = roundtrip(before)

    for name, reg in before.modules.items():
        other = after.modules[name]
        for candidate in NAMES:
            assert reg._include_pred(candidate) == other._include_pred(candidate)
            assert reg._exclude_pred(candidate) == other._exclude_pred(candidate)
            assert reg._include_qual_pred(candidate) == other._include_qual_pred(
                candidate
            )
            assert reg._exclude_qual_pred(candidate) == other._exclude_qual_pred(
                candidate
            )

    for name, reg in before.classes.items():
        other = after.classes[name]
        for candidate in NAMES:
            assert reg._include_pred(candidate) == other._include_pred(candidate)
            assert reg._exclude_pred(candidate) == other._exclude_pred(candidate)


# -- module grants cross by name ---------------------------------------------


def test_module_grants_resolve_to_the_same_modules():
    after = roundtrip(build_policy())
    assert after.resolve_module("math") is math
    assert after.resolve_module("string") is string
    assert after.resolve_module("urllib.parse") is urllib.parse


def test_scalars_and_root_survive():
    after = roundtrip(build_policy())
    assert after.timeout == 7.5
    assert after.tick_limit == 1234
    assert after.module_root == "/workspace"


# -- the id()-keyed indexes ---------------------------------------------------


def test_state_does_not_carry_the_id_maps():
    """Pinned structurally, because behaviour can't pin it in-process.

    A same-process round trip resolves ``math`` to the same module object and
    ``Point`` to the same class, so ids match by luck and a carried-over map
    keeps working. Only another process exposes it -- see the cross-process
    test below -- and this assertion is the cheap guard that fails the moment
    ``__getstate__`` starts shipping the maps again.
    """
    state = build_policy().__getstate__()
    assert "_reg_by_cls_id" not in state
    assert "_reg_by_module_id" not in state


def test_indexes_are_rebuilt_against_the_new_objects():
    """The failure this whole file exists for: ints pickle fine, so carrying
    the maps across yields a policy that pickles and is wrong."""
    after = roundtrip(build_policy())

    assert set(after._reg_by_module_id) == {id(r.obj) for r in after.modules.values()}
    assert set(after._reg_by_cls_id) == {id(r.cls) for r in after.classes.values()}

    for reg in after.modules.values():
        assert after._reg_by_module_id[id(reg.obj)] is reg
    for reg in after.classes.values():
        assert after._reg_by_cls_id[id(reg.cls)] is reg


def test_find_registration_agrees_across_the_round_trip():
    before = build_policy()
    after = roundtrip(before)

    for probe in (math, string, urllib.parse, Point, Point(3)):
        b = before._find_registration_for(probe)
        a = after._find_registration_for(probe)
        assert (b is None) == (a is None), f"{probe!r} disagreed"
        if b is not None:
            assert a.name == b.name


def test_attribute_decisions_agree_across_the_round_trip():
    before = build_policy()
    after = roundtrip(before)

    probes = [
        (math, "sqrt"),
        (math, "_unknown"),
        (string, "capwords"),
        (string, "digits"),
        (Point, "scale"),
        (Point, "_private"),
        (Point(1), "scale"),
        (urllib.parse, "urlparse"),
    ]
    for obj, attr in probes:
        assert before.is_attr_allowed(obj, attr) == after.is_attr_allowed(obj, attr), (
            f"{obj!r}.{attr} disagreed"
        )


def test_import_decisions_agree_across_the_round_trip():
    before = build_policy()
    after = roundtrip(before)

    for name in (
        "math",
        "string",
        "urllib.parse",
        "urllib.parse.quote",
        "urllib",
        "os",
        "urllib.parse._implicit_encoding",
    ):
        assert before.is_import_allowed(name) == after.is_import_allowed(name), (
            f"{name} disagreed"
        )


def test_needs_flags_agree_across_the_round_trip():
    before = build_policy()
    after = roundtrip(before)
    assert before.needs_network() == after.needs_network()
    assert before.needs_host_fs() == after.needs_host_fs()


# -- across a real process boundary ------------------------------------------
#
# The point of the exercise. A same-process round trip cannot detect a stale
# id() map, because the objects come back at the same addresses; only a process
# that built its own ``math`` and its own ``Point`` can. One corpus, evaluated
# on both sides, so a probe added here is automatically checked in both.

PROBE_LABELS = ("math", "string", "urllib.parse", "Point", "point-instance")
ATTRS = ("sqrt", "capwords", "digits", "scale", "_private", "urlparse", "__class__")
IMPORT_NAMES = (
    "math",
    "string",
    "urllib.parse",
    "urllib.parse.quote",
    "urllib",
    "os",
    "urllib.parse._implicit_encoding",
)
MEMBERS = (
    ("math", "sqrt"),
    ("string", "capwords"),
    ("string", "digits"),
    ("urllib.parse", "urlparse"),
    ("os", "system"),
)


def _probe_object(label: str):
    import string as _string
    import urllib.parse as _urlparse

    return {
        "math": math,
        "string": _string,
        "urllib.parse": _urlparse,
        "Point": Point,
        "point-instance": Point(3),
    }[label]


def _decisions(policy) -> dict:
    """Every decision the policy can make without host objects, flattened.

    Values only -- comparing dicts gives a readable diff naming the exact probe
    that disagreed, which matters because the interesting direction is
    deny -> allow.
    """
    out: dict = {}
    for label in PROBE_LABELS:
        obj = _probe_object(label)
        for attr in ATTRS:
            out[f"attr:{label}.{attr}"] = policy.is_attr_allowed(obj, attr)
        reg = policy._find_registration_for(obj)
        out[f"reg:{label}"] = reg.name if reg is not None else None

    for name in IMPORT_NAMES:
        out[f"import:{name}"] = policy.is_import_allowed(name)
        try:
            out[f"resolve:{name}"] = policy.resolve_module(name).__name__
        except Exception as exc:
            out[f"resolve:{name}"] = type(exc).__name__

    for module_name, member in MEMBERS:
        try:
            policy.resolve_module_member(module_name, member)
            out[f"member:{module_name}.{member}"] = True
        except Exception as exc:
            out[f"member:{module_name}.{member}"] = type(exc).__name__

    out["needs_network"] = policy.needs_network()
    out["needs_host_fs"] = policy.needs_host_fs()
    return out


def _decide_in_child(conn, payload: bytes) -> None:
    """Runs in a SPAWNED process: no inherited memory, fresh addresses."""
    import pickle as _pickle

    conn.send(_decisions(_pickle.loads(payload)))
    conn.close()


def test_a_spawned_process_decides_identically():
    """The real assertion. A carried-over id() map survives a same-process
    round trip untouched but misses every lookup here, so registrations come
    back None and the policy silently stops applying."""
    import multiprocessing as mp

    policy = build_policy()
    expected = _decisions(policy)
    assert len(expected) > 40, "corpus shrank -- differential is weaker than it looks"

    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_decide_in_child, args=(child_conn, pickle.dumps(policy)))
    proc.start()
    child_conn.close()
    try:
        assert parent_conn.poll(60), "spawned child never reported"
        actual = parent_conn.recv()
    finally:
        proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()

    assert actual == expected


# -- what must still fail, and fail clearly ----------------------------------


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_live_object_grant_refuses_to_pickle():
    """A live object can't cross to a non-forked worker. Under fork it
    "works" by handing the child a copy-on-write snapshot whose mutations
    never reach the host -- so refusing is the fix, not the regression."""

    class Counter:
        def __init__(self):
            self.n = 0

    p = Policy()
    p.module(Counter(), name="live")

    with pytest.raises(pickle.PicklingError) as exc:
        pickle.dumps(p)
    message = str(exc.value)
    assert "live" in message  # names the registration
    assert "rpc" in message.lower()  # points at the supported path


def test_callable_pattern_fails_on_the_callable():
    """Not on the derived predicate -- the user needs to know it's their
    filter, not sandtrap's internals."""
    p = Policy()
    p.module(math, include=lambda name: name.startswith("s"))

    with pytest.raises((pickle.PicklingError, AttributeError, TypeError)) as exc:
        pickle.dumps(p)
    assert "_make_predicate" not in str(exc.value)


def test_unimportable_module_grant_names_itself_on_load():
    import types

    ghost = types.ModuleType("sandtrap_ghost_module")
    p = Policy()
    p.module(ghost)

    data = pickle.dumps(p)  # dumping is fine -- it's a real module object
    with pytest.raises(Exception) as exc:
        pickle.loads(data)
    assert "sandtrap_ghost_module" in str(exc.value)


def test_unpicklable_function_grant_still_fails():
    p = Policy()
    p.fn(lambda: None, name="anon")
    with pytest.raises((pickle.PicklingError, AttributeError)):
        pickle.dumps(p)


class Service:
    """A host resource of the kind ``policy.fn`` exists to expose."""

    def __init__(self) -> None:
        self.calls: list = []

    def record(self, value):
        self.calls.append(value)
        return len(self.calls)

    def __call__(self, value):
        return self.record(value)

    @classmethod
    def described(cls) -> str:
        return cls.__name__


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_bound_method_is_refused_rather_than_copied():
    """It *would* pickle -- by copying the instance -- so the worker would
    call a copy and the host would never see it. Same silent divergence as a
    live-object module grant, arriving through the fn door."""
    svc = Service()
    p = Policy()
    p.fn(svc.record, name="record")

    with pytest.raises(pickle.PicklingError) as exc:
        pickle.dumps(p)
    message = str(exc.value)
    assert "record" in message  # names the registration
    assert "Service" in message  # names what it's bound to
    assert "rpc" in message.lower()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_callable_instance_is_refused():
    p = Policy()
    p.fn(Service(), name="svc")
    with pytest.raises(pickle.PicklingError, match="callable Service instance"):
        pickle.dumps(p)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_partial_is_only_as_bridgeable_as_what_it_wraps():
    import functools

    ok = Policy()
    ok.fn(functools.partial(len), name="length")
    pickle.loads(pickle.dumps(ok))  # wraps a builtin -- crosses by name

    bad = Policy()
    bad.fn(functools.partial(Service().record), name="record")
    with pytest.raises(pickle.PicklingError, match="bound to a live"):
        pickle.dumps(bad)


def test_by_reference_callables_are_untouched():
    """The guard must not fire on the forms that genuinely cross by name, or
    it would break every ordinary registration."""
    p = Policy()
    p.fn(len, name="length")  # builtin
    p.fn(Service.described, name="described")  # classmethod -> binds to the class
    p.fn(Service.record, name="unbound")  # plain function off the class
    after = roundtrip(p)
    assert set(after.functions) == {"length", "described", "unbound"}
    assert after.functions["length"].func is len


# -- deprecation of live-object grants ---------------------------------------
#
# The narrowing: a host object is exposed by registering its CLASS and binding
# the instance, not by putting the instance in the policy. Same filters, same
# per-member privileges, and the policy stays picklable -- but one pattern
# instead of two with different semantics per isolation level.


class Narrow:
    def allowed(self):
        return "allowed"

    def forbidden(self):
        return "forbidden"

    token = "sekrit"


def test_live_object_module_grant_warns():
    p = Policy()
    with pytest.warns(DeprecationWarning) as record:
        p.module(Narrow(), name="c")
    message = str(record[0].message)
    assert "policy.cls(" in message  # names the replacement
    assert "import c" in message  # names what stops working
    assert "COPY" in message  # says why it isn't a loss


def test_live_callable_fn_grant_warns():
    p = Policy()
    with pytest.warns(DeprecationWarning, match="bound to a live Narrow instance"):
        p.fn(Narrow().allowed, name="allowed")


def test_by_reference_registrations_do_not_warn():
    """The common cases must stay quiet, or the warning is noise people
    learn to filter."""
    import math
    import warnings as _warnings

    p = Policy()
    with _warnings.catch_warnings():
        _warnings.simplefilter("error", DeprecationWarning)
        p.module(math)
        p.cls(Narrow)
        p.fn(len, name="length")

        @p.fn
        def helper():
            return 1


def test_the_replacement_pattern_is_equivalent():
    """The migration has to actually work, or the warning is telling people to
    do something broken."""
    from sandtrap import Sandbox

    obj = Narrow()
    p = Policy(timeout=5.0)
    p.cls(Narrow, include=("allowed",))

    with Sandbox(p) as sb:
        assert sb.exec("v = c.allowed()", namespace={"c": obj}).namespace["v"] == (
            "allowed"
        )
        assert sb.exec("v = c.forbidden()", namespace={"c": obj}).error is not None
        assert sb.exec("v = c.token", namespace={"c": obj}).error is not None

    roundtrip(p)  # ...and unlike the deprecated form, it survives pickling
