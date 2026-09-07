"""Tests for the unified sandbox() factory."""

import asyncio
import threading

import pytest
from monkeyfs import IsolatedFS, VirtualFS

from sandtrap import Policy, StPolicyNotPortable, sandbox
from sandtrap.process.sandbox import ProcessSandbox
from sandtrap.sandbox import Sandbox
from sandtrap.wrappers import StFunction


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


# ------------------------------------------------------------------
# isolation="none" (in-process)
# ------------------------------------------------------------------


def test_none_returns_sandbox():
    sb = sandbox(Policy(timeout=5.0))
    assert isinstance(sb, Sandbox)


def test_none_basic_exec():
    with sandbox(Policy(timeout=5.0)) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_none_with_filesystem():
    fs = VirtualFS({})
    fs.write("/data.txt", b"hello")
    with sandbox(Policy(timeout=5.0), filesystem=fs) as sb:
        result = sb.exec("content = open('/data.txt').read()")
        assert result.error is None
        assert result.namespace["content"] == "hello"


# ------------------------------------------------------------------
# isolation="process" (subprocess, no kernel)
# ------------------------------------------------------------------


def test_process_returns_process_sandbox():
    sb = sandbox(Policy(timeout=5.0), isolation="process")
    assert isinstance(sb, ProcessSandbox)


def test_process_forwards_close_fds():
    sb = sandbox(Policy(timeout=5.0), isolation="process", close_fds=True)
    assert isinstance(sb, ProcessSandbox)
    assert sb._close_fds is True


def test_process_forwards_start_method():
    sb = sandbox(Policy(timeout=5.0), isolation="process", start_method="spawn")
    assert sb._start_method == "spawn"


def test_process_forwards_preload_grants():
    sb = sandbox(Policy(timeout=5.0), isolation="process", preload_grants=True)
    assert sb._preload_grants is True


def test_start_method_defaults_to_the_safe_one():
    """Unset must keep meaning "whatever is safest here" — forwarding None
    rather than a literal would pin the factory to one platform's choice."""
    from sandtrap.process.sandbox import default_start_method

    sb = sandbox(Policy(timeout=5.0), isolation="process")
    assert sb._start_method == default_start_method()


@pytest.mark.parametrize("isolation", ["process", "kernel"])
def test_the_unportable_policy_escape_hatch_is_reachable(isolation):
    """The point of forwarding ``start_method``.

    A policy holding a live object can't be serialized to a non-forked
    worker, so 0.3.0 refuses it with ``StPolicyNotPortable`` — and names
    ``start_method="fork"`` as the way out. ``sandbox()`` is the only
    constructor in ``sandtrap.__all__``, so if it can't express that, the
    documented remedy is unreachable from the public API.
    """

    class Live:
        def ping(self):
            return "pong"

    policy = Policy(timeout=5.0)
    with pytest.warns(DeprecationWarning):  # live-object grants are deprecated
        policy.module(Live(), name="live")

    with pytest.raises(StPolicyNotPortable):
        sandbox(policy, isolation=isolation)

    # ...and the escape hatch works, from the public API, with no reach
    # into sandtrap.process.
    sb = sandbox(policy, isolation=isolation, start_method="fork")
    assert sb._start_method == "fork"


def test_start_method_is_ignored_without_a_worker():
    """``isolation="none"`` has no worker to create — same posture as
    ``rpc_handlers`` and ``close_fds``: accepted and ignored, not an error,
    so one config can drive every rung."""
    sb = sandbox(Policy(timeout=5.0), start_method="spawn", preload_grants=True)
    assert isinstance(sb, Sandbox)


def test_process_basic_exec(root):
    with sandbox(
        Policy(timeout=5.0), isolation="process", filesystem=IsolatedFS(root)
    ) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_process_no_filesystem():
    with sandbox(Policy(timeout=5.0), isolation="process") as sb:
        result = sb.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


def test_process_with_virtualfs():
    fs = VirtualFS({})
    fs.write("/data.txt", b"hello")
    with sandbox(Policy(timeout=5.0), isolation="process", filesystem=fs) as sb:
        result = sb.exec("content = open('/data.txt').read()")
        assert result.error is None
        assert result.namespace["content"] == "hello"


# ------------------------------------------------------------------
# isolation="kernel" (subprocess + kernel restrictions)
# ------------------------------------------------------------------


def test_kernel_returns_process_sandbox():
    sb = sandbox(Policy(timeout=5.0), isolation="kernel")
    assert isinstance(sb, ProcessSandbox)


def test_kernel_basic_exec(root):
    with sandbox(
        Policy(timeout=5.0), isolation="kernel", filesystem=IsolatedFS(root)
    ) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_kernel_no_filesystem():
    with sandbox(Policy(timeout=5.0), isolation="kernel") as sb:
        result = sb.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


# ------------------------------------------------------------------
# snapshot_prints
# ------------------------------------------------------------------


def test_snapshot_prints_default_empty():
    with sandbox(Policy(timeout=5.0)) as sb:
        result = sb.exec("print('hello')")
        assert result.error is None
        assert result.prints == []
        assert result.stdout == "hello\n"


def test_snapshot_prints_none():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
        result = sb.exec("print('hello', 42)")
        assert result.error is None
        assert len(result.prints) == 1
        assert result.prints[0] == ("hello", 42)


def test_snapshot_prints_mutation_safe():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
        result = sb.exec(
            """\
data = [1, 2, 3]
print(data)
data.clear()
"""
        )
        assert result.error is None
        assert result.prints[0] == ([1, 2, 3],)


def test_snapshot_prints_process(root):
    with sandbox(
        Policy(timeout=5.0),
        isolation="process",
        filesystem=IsolatedFS(root),
        snapshot_prints=True,
    ) as sb:
        result = sb.exec("print('cross-process', 99)")
        assert result.error is None
        assert result.prints == [("cross-process", 99)]


def test_snapshot_prints_kernel(root):
    with sandbox(
        Policy(timeout=5.0),
        isolation="kernel",
        filesystem=IsolatedFS(root),
        snapshot_prints=True,
    ) as sb:
        result = sb.exec("print('kernel', True)")
        assert result.error is None
        assert result.prints == [("kernel", True)]


def test_stdout_always_captured_with_snapshot():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
        result = sb.exec("print('hello')")
        assert result.stdout == "hello\n"
        assert result.prints == [("hello",)]


def test_snapshot_prints_multiple():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
        result = sb.exec("print('a')\nprint('b', 'c')")
        assert result.prints == [("a",), ("b", "c")]


def test_snapshot_prints_deepcopy_failure_fallback():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True, mode="raw") as sb:
        # __deepcopy__ raises — fallback captures raw reference
        result = sb.exec(
            """\
class NoCopy:
    def __deepcopy__(self, memo):
        raise TypeError("cannot copy")
obj = NoCopy()
print(obj)
"""
        )
        assert result.error is None
        assert len(result.prints) == 1
        # Fallback kept the raw reference (not deep-copied)
        assert result.prints[0][0] is result.namespace["obj"]


def test_snapshot_prints_aexec():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
        result = asyncio.run(sb.aexec("print('async', 123)"))
        assert result.error is None
        assert result.prints == [("async", 123)]


def test_snapshot_prints_partial_on_error():
    with sandbox(Policy(timeout=5.0), snapshot_prints=True) as sb:
        result = sb.exec("print('before')\n1/0")
        assert isinstance(result.error, ZeroDivisionError)
        assert result.prints == [("before",)]


def test_snapshot_prints_unpicklable_filtered_cross_process(root):
    """Unpicklable print args are silently dropped in process mode."""
    policy = Policy(timeout=5.0)
    policy.module(threading, name="threading")
    with sandbox(
        policy,
        isolation="process",
        filesystem=IsolatedFS(root),
        snapshot_prints=True,
    ) as sb:
        result = sb.exec(
            """\
print('good', 42)
print(threading.Lock())
print('also good')
"""
        )
        assert result.error is None
        # Lock() can't pickle — that entry is dropped, others survive
        assert ("good", 42) in result.prints
        assert ("also good",) in result.prints


# ------------------------------------------------------------------
# mode parameter
# ------------------------------------------------------------------


def test_factory_default_mode_is_raw():
    """``sandbox()`` with no ``mode`` returns plain functions."""
    with sandbox(Policy(timeout=5.0)) as sb:
        result = sb.exec("def f(x): return x + 1")
        assert result.error is None
        assert not isinstance(result.namespace["f"], StFunction)
        assert result.namespace["f"](1) == 2


def test_factory_wrapped_mode_warns():
    with pytest.warns(DeprecationWarning, match="deprecated"):
        sandbox(Policy(timeout=5.0), mode="wrapped")


def test_factory_raw_mode_does_not_warn(recwarn):
    sandbox(Policy(timeout=5.0), mode="raw")
    assert [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)] == []


def test_mode_raw_none():
    with sandbox(Policy(timeout=5.0), mode="raw") as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_mode_raw_process(root):
    with sandbox(
        Policy(timeout=5.0),
        mode="raw",
        isolation="process",
        filesystem=IsolatedFS(root),
    ) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5
