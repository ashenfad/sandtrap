"""Pre-flight: can this policy reach a worker that isn't forked from here?

``pickle`` answers one failure at a time, from inside the serializer, with no
idea which registration caused it — so fixing a policy that way is a loop of
rerun, read, fix, for something knowable up front. ``check_picklable`` reports
all of it at once, and ``ProcessSandbox`` raises on it at construction, where
the embedder can still act.

Note what the check is *not*: "does it pickle". A module built at runtime
serializes happily and fails to load on the other side, which is precisely the
case a pre-flight check exists to catch.
"""

import math
import pickle
import types
import warnings

import pytest

from sandtrap import Policy, PolicyProblem, StPolicyNotPortable
from sandtrap.process.sandbox import ProcessSandbox


class Service:
    def go(self):
        return 1


def kinds(policy: Policy) -> set[str]:
    return {problem.kind for problem in policy.check_picklable()}


def test_a_portable_policy_reports_nothing():
    p = Policy(timeout=5.0)
    p.module(math)
    p.cls(Service)
    p.fn(len, name="length")
    assert p.check_picklable() == []


# -- the four named diagnoses -------------------------------------------------


def test_unimportable_module_is_caught_though_it_pickles():
    """The false negative a shallow "does it dumps()" check would miss."""
    p = Policy(timeout=5.0)
    p.module(types.ModuleType("sandtrap_ghost_for_test"))

    pickle.dumps(p)  # dumping is fine — that's the trap
    problems = p.check_picklable()
    assert [prob.kind for prob in problems] == ["unimportable module"]
    assert "sandtrap_ghost_for_test" in problems[0].detail


def test_synthetic_module_registered_in_sys_modules_is_still_caught():
    """find_spec raises rather than returning None for a module with no
    __spec__, and both mean the same thing here."""
    import sys

    name = "sandtrap_ghost_in_sys_modules"
    sys.modules[name] = types.ModuleType(name)
    try:
        p = Policy(timeout=5.0)
        p.module(sys.modules[name])
        assert kinds(p) == {"unimportable module"}
    finally:
        del sys.modules[name]


def test_live_object_grant_is_named():
    p = Policy(timeout=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p.module(Service(), name="svc")
    problems = p.check_picklable()
    assert [prob.kind for prob in problems] == ["live-object grant"]
    assert "Register the class" in problems[0].remedy


def test_live_callable_is_named():
    p = Policy(timeout=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p.fn(Service().go, name="go")
    assert kinds(p) == {"live callable"}


def test_callable_filter_is_named_once():
    """A callable predicate would also fail to pickle; reporting the same
    cause twice makes the list harder to act on, not easier."""
    p = Policy(timeout=5.0)
    p.module(math, name="filtered", include=lambda name: True)
    problems = p.check_picklable()
    assert [prob.kind for prob in problems] == ["callable filter"]
    assert "include=" in problems[0].detail


def test_anything_else_falls_back_to_pickle_s_own_message():
    p = Policy(timeout=5.0)
    p.fn(lambda: None, name="anon")
    problems = p.check_picklable()
    assert [prob.kind for prob in problems] == ["unpicklable"]
    assert "lambda" in problems[0].detail


# -- reporting everything, not the first --------------------------------------


def test_every_problem_is_reported_in_one_pass():
    """The reason this exists at all."""
    p = Policy(timeout=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p.module(math)  # fine
        p.module(types.ModuleType("sandtrap_ghost_multi"))
        p.module(Service(), name="svc")
        p.fn(Service().go, name="go")
        p.fn(lambda: None, name="anon")

    assert kinds(p) == {
        "unimportable module",
        "live-object grant",
        "live callable",
        "unpicklable",
    }
    assert all(isinstance(prob, PolicyProblem) for prob in p.check_picklable())


def test_problems_name_their_registration():
    p = Policy(timeout=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p.module(Service(), name="my_service")
    assert p.check_picklable()[0].name == "my_service"


# -- construction-time enforcement --------------------------------------------


def test_non_fork_construction_raises_with_the_whole_list():
    p = Policy(timeout=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p.module(Service(), name="svc")
        p.fn(lambda: None, name="anon")

    with pytest.raises(StPolicyNotPortable) as exc:
        ProcessSandbox(p, isolation="none", start_method="spawn")

    assert len(exc.value.problems) == 2
    message = str(exc.value)
    assert "svc" in message and "anon" in message
    assert 'start_method="fork"' in message  # the escape hatch


@pytest.mark.fork_only  # asserts fork's exemption, which this run overrides
def test_fork_construction_is_unaffected():
    """Fork inherits memory, so none of this applies — and adding a check
    there would break every existing embedder."""
    p = Policy(timeout=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p.module(Service(), name="svc")
    ProcessSandbox(p, isolation="none")  # start_method="fork" by default


def test_a_portable_policy_constructs_under_spawn():
    p = Policy(timeout=15.0)
    p.module(math)
    p.cls(Service)
    with ProcessSandbox(p, isolation="none", start_method="spawn") as sb:
        assert sb.exec("import math\nv = math.sqrt(4)").namespace["v"] == 2.0


def test_it_subclasses_value_error():
    """So ordinary construction-error handling keeps working."""
    assert issubclass(StPolicyNotPortable, ValueError)


# -- partials bind as well as wrap --------------------------------------------


def test_a_partial_binding_a_live_callable_is_caught():
    """Pickling a partial pickles its ARGUMENTS too, so
    ``partial(use, service.record)`` copies the service into the worker even
    though ``use`` itself crosses by name."""
    import functools

    p = Policy(timeout=5.0)
    p.fn(functools.partial(len, Service().go), name="rec")
    problems = p.check_picklable()
    assert [prob.kind for prob in problems] == ["live callable"]
    assert "partial binding" in problems[0].detail
    assert "Service" in problems[0].detail


def test_a_partial_binding_plain_data_stays_portable():
    """The guard must not fire on ordinary configuration, or it would reject
    portable policies."""
    import functools

    p = Policy(timeout=5.0)
    p.fn(functools.partial(len, [1, 2, 3]), name="counted")
    assert p.check_picklable() == []
