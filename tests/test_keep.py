"""Tests for choosing what an execution's result carries.

``keep`` names the bindings ``result.namespace`` carries. A ``MemoryError``
carries none of them, nor any print snapshot, so reporting a memory
failure never costs a pickled copy of what caused it.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import weakref

import pytest

from sandtrap import IsolationUnavailable, Policy, sandbox

ISOLATIONS = ("none", "process", "kernel")
RUNNERS = ("exec", "aexec")


def _sandbox(isolation, *, policy=None, snapshot_prints=False):
    try:
        return sandbox(
            policy or Policy(timeout=30.0),
            isolation=isolation,
            snapshot_prints=snapshot_prints,
        )
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


def _run(sb, runner, source, **kwargs):
    if runner == "aexec":
        return asyncio.run(sb.aexec(source, **kwargs))
    return sb.exec(source, **kwargs)


# ------------------------------------------------------------------
# keep
# ------------------------------------------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_keep_returns_only_the_named_bindings(isolation, runner):
    with _sandbox(isolation) as sb:
        result = _run(sb, runner, "a = 1\nb = 2\nc = 3\n", keep=("a", "c"))
    assert result.error is None, result.error
    assert result.namespace == {"a": 1, "c": 3}
    assert result.dropped == ()


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_without_keep_every_binding_comes_back(isolation, runner):
    with _sandbox(isolation) as sb:
        result = _run(sb, runner, "a = 1\nb = 2\n")
    assert result.error is None, result.error
    assert result.namespace == {"a": 1, "b": 2}


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_kept_name_the_code_never_bound_is_simply_absent(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec("a = 1\n", keep=("a", "ui"))
    assert result.namespace == {"a": 1}
    assert result.dropped == ()


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_empty_keep_returns_nothing(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec("a = 1\n", keep=())
    assert result.error is None, result.error
    assert result.namespace == {}


@pytest.mark.parametrize("isolation", ("process", "kernel"))
def test_a_name_left_out_is_never_pickled(isolation):
    """An unpicklable value outside ``keep`` is not reported as dropped:
    the worker never tried to send it."""
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "fn = lambda: 1\nkept = lambda: 2\nn = 3\n", keep=("kept", "n")
        )
    assert result.namespace == {"n": 3}
    assert result.dropped == ("kept",)


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize(
    "keep, message",
    [
        ("ui", "collection of names"),
        (b"ui", "collection of names"),
        (42, "collection of names"),
        (("ui", 3), "must be strings"),
    ],
)
def test_keep_refuses_what_is_not_a_collection_of_names(isolation, keep, message):
    with _sandbox(isolation) as sb:
        with pytest.raises(TypeError, match=message):
            sb.exec("a = 1\n", keep=keep)


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_aexec_refuses_a_bad_keep_too(isolation):
    with _sandbox(isolation) as sb:
        with pytest.raises(TypeError, match="collection of names"):
            asyncio.run(sb.aexec("a = 1\n", keep="ui"))


# ------------------------------------------------------------------
# MemoryError
# ------------------------------------------------------------------

_MEMORY_FAILURE = (
    "scratch = bytearray(1 << 20)\n"
    "label = 'big'\n"
    "print('allocated', len(scratch))\n"
    "raise MemoryError('synthetic')\n"
)


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_memory_error_carries_no_namespace_and_no_prints(isolation, runner):
    with _sandbox(isolation, snapshot_prints=True) as sb:
        result = _run(sb, runner, _MEMORY_FAILURE)
    assert isinstance(result.error, MemoryError)
    assert "synthetic" in str(result.error)
    assert result.namespace == {}
    assert result.prints == []
    assert "scratch" in result.dropped and "label" in result.dropped
    # the captured text still comes back
    assert "allocated 1048576" in result.stdout


@pytest.mark.parametrize("isolation", ("process", "kernel"))
def test_a_memory_error_still_carries_its_traceback(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(_MEMORY_FAILURE)
    rendered = getattr(result.error, "_st_traceback_text", "")
    assert "MemoryError: synthetic" in rendered
    assert "line 4" in rendered


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_memory_error_lists_only_the_kept_names_as_withheld(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(_MEMORY_FAILURE, keep=("label",))
    assert result.namespace == {}
    assert result.dropped == ("label",)


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_ordinary_error_still_returns_the_namespace(isolation, runner):
    with _sandbox(isolation, snapshot_prints=True) as sb:
        result = _run(sb, runner, "x = 1\nprint(x)\nraise ValueError('boom')\n")
    assert isinstance(result.error, ValueError)
    assert result.namespace == {"x": 1}
    assert result.prints == [(1,)]
    assert result.dropped == ()


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_the_sandbox_works_after_a_memory_error(isolation, runner):
    with _sandbox(isolation) as sb:
        failed = _run(sb, runner, _MEMORY_FAILURE)
        assert isinstance(failed.error, MemoryError)
        result = _run(sb, runner, "y = 2\nprint('again')\n")
    assert result.error is None, result.error
    assert result.namespace == {"y": 2}
    assert result.stdout == "again\n"


def test_a_memory_error_lets_go_of_the_executions_module():
    """In-process, the failed execution's module leaves ``sys.modules`` at
    once, and once the result is dropped nothing holds what the code built:
    no collection is needed to free it."""
    held = []

    def track(obj):
        held.append(weakref.ref(obj))

    policy = Policy(timeout=30.0)
    policy.fn(track)
    with _sandbox("none", policy=policy) as sb:
        enabled = gc.isenabled()
        gc.disable()
        try:
            result = sb.exec(
                "class Big:\n    pass\nb = Big()\ntrack(b)\n"
                "raise MemoryError('synthetic')\n"
            )
            assert sb.module_name not in sys.modules
            assert held[0]() is not None  # the error's traceback still has it
            del result
            assert held[0]() is None
        finally:
            if enabled:
                gc.enable()


@pytest.mark.parametrize("runner", RUNNERS)
def test_rebinding_name_does_not_keep_a_failed_module(runner):
    """The module is let go by the name it was registered under, which
    the code cannot change by rebinding ``__name__``."""
    with _sandbox("none") as sb:
        result = _run(sb, runner, "__name__ = 'mine'\nraise MemoryError('synthetic')\n")
        assert isinstance(result.error, MemoryError)
        assert sb.module_name not in sys.modules


class _RecordingGC:
    """Stands in for the worker's ``gc``: each ``collect()`` appends a
    line to *path*, which the parent can read after the worker ran."""

    def __init__(self, path):
        self.path = str(path)

    def collect(self):
        with open(self.path, "a") as f:
            f.write("collect\n")
        return 0


@pytest.mark.parametrize(
    "raise_it",
    [
        "raise MemoryError('synthetic')",
        # a class of the code's own can't be pickled out, so the worker
        # sends a stand-in error -- but the run still failed for memory
        "class Exhausted(MemoryError):\n    pass\nraise Exhausted('synthetic')",
    ],
    ids=["builtin", "sandbox-subclass"],
)
def test_the_worker_collects_after_a_memory_failure(tmp_path, monkeypatch, raise_it):
    import sandtrap.process.worker as worker

    record = tmp_path / "collected"
    monkeypatch.setattr(worker, "gc", _RecordingGC(record))
    with sandbox(Policy(timeout=30.0), isolation="process", start_method="fork") as sb:
        ok = sb.exec("x = 1\n")
        assert not record.exists()  # only a memory failure collects
        result = sb.exec(f"scratch = bytearray(1 << 20)\n{raise_it}\n")
    assert ok.error is None
    assert result.error is not None and "synthetic" in str(result.error)
    assert result.namespace == {}
    assert "scratch" in result.dropped
    assert record.read_text() == "collect\n"


def test_an_ordinary_error_keeps_the_executions_module():
    with _sandbox("none") as sb:
        result = sb.exec("x = 1\nraise ValueError('boom')\n")
        assert isinstance(result.error, ValueError)
        assert sb.module_name in sys.modules


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="RLIMIT_AS is enforced on Linux only",
)
@pytest.mark.parametrize("isolation", ("process", "kernel"))
def test_a_kernel_memory_limit_failure_carries_nothing_back(isolation):
    with _sandbox(isolation, policy=Policy(timeout=60.0, memory_limit=50)) as sb:
        result = sb.exec("scratch = bytearray(10 << 20)\nbig = bytearray(400 << 20)\n")
        assert isinstance(result.error, MemoryError)
        assert result.namespace == {}
        assert result.dropped == ("scratch",)
        after = sb.exec("y = 2\n")
    assert after.error is None, after.error
    assert after.namespace == {"y": 2}
