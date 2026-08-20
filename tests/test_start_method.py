"""Workers created without forking the embedding process.

Fork duplicates only the calling thread, so a lock another thread holds at
that instant is inherited already-held by a child with no thread to release
it — the child then hangs rather than crashing. ``start_method`` is the seam
for creating workers that don't inherit memory at all
(``docs/forkserver-design.md``).

Two mechanisms in ``_ensure_worker`` exist purely to undo fork inheritance and
must not run otherwise: descriptor neutralization (which works off the
*parent's* fd numbers) and the parent-connection registry (which a spawned
child would receive pickled — duplicating endpoints rather than closing
copies). These tests pin the behavior each way.
"""

import math
import string

import pytest

from sandtrap import Policy
from sandtrap.process.sandbox import ProcessSandbox

NON_FORK = ["spawn", "forkserver"]


def make_policy() -> Policy:
    """Grants that exercise the pickling path: a module, a filtered module,
    a builtin function."""
    p = Policy(timeout=15.0)
    p.module(math)
    p.module(string, include=("capwords",))
    p.fn(len, name="length")
    return p


# -- the seam itself ----------------------------------------------------------


def test_default_avoids_forking_this_process(pytestconfig):
    """The point of the default: a worker must not inherit our threads."""
    import multiprocessing

    if pytestconfig.getoption("--start-method") != "default":
        pytest.skip("this run overrides the shipped default")

    sb = ProcessSandbox(Policy(timeout=5.0), isolation="none")
    if "forkserver" in multiprocessing.get_all_start_methods():
        assert sb._start_method == "forkserver"
    else:
        assert sb._start_method == "spawn"
    assert sb._start_method != "fork"


def test_fork_remains_available_as_an_escape_hatch():
    """For policies that can't be serialized -- explicit, never silent."""
    sb = ProcessSandbox(Policy(timeout=5.0), isolation="none", start_method="fork")
    assert sb._start_method == "fork"


def test_unavailable_start_method_is_a_construction_error():
    """Not a worker failure at first exec — the embedder mistyped, and should
    hear about it where they can fix it."""
    with pytest.raises(ValueError, match="not available on this platform"):
        ProcessSandbox(Policy(timeout=5.0), isolation="none", start_method="wormhole")


# -- end to end ---------------------------------------------------------------


@pytest.mark.parametrize("start_method", NON_FORK)
def test_worker_runs_without_inheriting_memory(start_method):
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method=start_method
    ) as sb:
        result = sb.exec("x = 1 + 1")
        assert result.error is None
        assert result.namespace["x"] == 2


@pytest.mark.parametrize("start_method", NON_FORK)
def test_granted_modules_survive_the_crossing(start_method):
    """The payoff: a policy whose grants had to be serialized and re-imported
    still gates the worker the way it was written."""
    with ProcessSandbox(
        make_policy(), isolation="none", start_method=start_method
    ) as sb:
        allowed = sb.exec("import math\nv = math.sqrt(16)")
        assert allowed.error is None
        assert allowed.namespace["v"] == 4.0

        filtered = sb.exec("import string\nv = string.capwords('a b')")
        assert filtered.error is None
        assert filtered.namespace["v"] == "A B"

        # include=("capwords",) — the filter has to survive too, or the policy
        # silently widened in transit.
        excluded = sb.exec("import string\nv = string.digits")
        assert excluded.error is not None

        ungranted = sb.exec("import os")
        assert ungranted.error is not None


@pytest.mark.parametrize("start_method", NON_FORK)
def test_close_fds_is_a_noop_rather_than_an_error(start_method):
    """nontainer passes close_fds=True unconditionally under process
    isolation. Rejecting it would break that for no benefit: a spawned child
    inherits nothing, so the guarantee already holds."""
    with ProcessSandbox(
        Policy(timeout=15.0),
        isolation="none",
        close_fds=True,
        start_method=start_method,
    ) as sb:
        assert sb.exec("x = 1").namespace["x"] == 1


@pytest.mark.parametrize("start_method", NON_FORK)
def test_two_live_sandboxes_dont_interfere(start_method):
    """The parent-connection registry is fork-only; a non-fork child gets an
    empty tuple. Two live workers is where passing it wrongly would show."""
    a = ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method=start_method
    )
    b = ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method=start_method
    )
    with a, b:
        assert a.exec("x = 'a'").namespace["x"] == "a"
        assert b.exec("x = 'b'").namespace["x"] == "b"
        assert a.exec("y = 1").namespace["y"] == 1  # still healthy after b ran


@pytest.mark.parametrize("start_method", NON_FORK)
def test_sandboxed_errors_are_still_results(start_method):
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method=start_method
    ) as sb:
        result = sb.exec("1 / 0")
        assert isinstance(result.error, ZeroDivisionError)


@pytest.mark.parametrize("start_method", NON_FORK)
def test_worker_runs_in_a_different_process(start_method):
    """Guards against a future refactor quietly falling back to in-process
    execution and passing everything above."""
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method=start_method
    ) as sb:
        assert sb._process is not None
        assert sb._process.pid != __import__("os").getpid()


def test_the_worker_really_does_not_inherit_this_process_memory():
    """Decisive, and the semantic difference docs have to state.

    Patch a granted module *after* building the policy. A forked worker
    inherits this process's memory and sees the patch; a spawned one
    re-imports by name and gets the pristine module. If spawn ever silently
    degraded to fork, this is what would catch it.
    """
    original = string.capwords
    policy = Policy(timeout=15.0)
    policy.module(string, include=("capwords",))
    string.capwords = lambda s, sep=None: "PATCHED"  # noqa: ARG005
    try:
        with ProcessSandbox(policy, isolation="none", start_method="fork") as sb:
            forked = sb.exec("import string\nv = string.capwords('a b')")
        with ProcessSandbox(policy, isolation="none", start_method="spawn") as sb:
            spawned = sb.exec("import string\nv = string.capwords('a b')")
    finally:
        string.capwords = original

    assert forked.namespace["v"] == "PATCHED"  # inherited the patched module
    assert spawned.namespace["v"] == "A B"  # re-imported a pristine one


# -- what a non-forked worker refuses ----------------------------------------


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_live_object_grant_is_refused_rather_than_silently_copied():
    """Under fork this "works" — by handing the worker a copy-on-write
    snapshot whose mutations never reach the host object. Without inherited
    memory there's nothing to copy, and the refusal is the honest outcome."""

    class Counter:
        def __init__(self):
            self.n = 0

    policy = Policy(timeout=15.0)
    policy.module(Counter(), name="live")

    # Refused at CONSTRUCTION, not at worker start: the policy is checked up
    # front so the embedder hears about it where they wrote it.
    with pytest.raises(Exception) as exc:
        ProcessSandbox(policy, isolation="none", start_method="spawn")
    assert "live" in str(exc.value)
