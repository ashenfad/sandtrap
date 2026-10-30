"""Tests for ``on_worker_start``: every worker is prepared before an
execution reaches it, the first and each replacement alike."""

from __future__ import annotations

import os
import signal

import pytest

from sandtrap import IsolationUnavailable, Policy, sandbox
from sandtrap.process.sandbox import ProcessSandbox

from . import worker_state

PREPARE = "import worker_state\nworker_state.prepare()"
STATE = "import worker_state\nstate = worker_state.state()"


def _policy(timeout=10.0):
    policy = Policy(timeout=timeout)
    policy.module(worker_state, name="worker_state")
    return policy


class Recorder:
    """A start hook that prepares the worker and notes each call."""

    def __init__(self, source=PREPARE, *, timeout=None):
        self.source = source
        self.timeout = timeout
        self.generations: list[int] = []
        self.fail: BaseException | None = None

    def __call__(self, sb):
        self.generations.append(sb.generation)
        if self.fail is not None:
            raise self.fail
        result = sb.exec(self.source, timeout=self.timeout)
        if result.error is not None:
            raise RuntimeError(f"setup failed: {result.error}")


def _state(sb):
    result = sb.exec(STATE)
    assert result.error is None, result.error
    return result.namespace["state"]


def _kill_worker(sb):
    os.kill(sb._process.pid, signal.SIGKILL)
    sb._process.join(timeout=5.0)


def test_the_first_worker_is_prepared_before_any_execution():
    hook = Recorder()
    with ProcessSandbox(_policy(), on_worker_start=hook) as sb:
        assert hook.generations == [1]
        pid, prepared_in = _state(sb)
        assert prepared_in == pid
        assert sb.generation == 1


def test_a_worker_that_replaces_a_dead_one_is_prepared_too():
    hook = Recorder()
    with ProcessSandbox(_policy(), on_worker_start=hook) as sb:
        first, _ = _state(sb)
        _kill_worker(sb)
        pid, prepared_in = _state(sb)
    assert pid != first
    assert prepared_in == pid
    assert hook.generations == [1, 2]


def test_re_entering_after_shutdown_prepares_the_new_worker():
    hook = Recorder()
    sb = ProcessSandbox(_policy(), on_worker_start=hook)
    with sb:
        first, _ = _state(sb)
    with sb:
        pid, prepared_in = _state(sb)
    assert pid != first and prepared_in == pid
    assert hook.generations == [1, 2]


def test_without_a_hook_a_replacement_starts_unprepared():
    with ProcessSandbox(_policy()) as sb:
        assert sb.exec(PREPARE).error is None
        _kill_worker(sb)
        _, prepared_in = _state(sb)
        assert prepared_in is None
        assert sb.generation == 2


def test_a_hook_that_raises_fails_the_enter_and_kills_the_worker():
    hook = Recorder()
    hook.fail = KeyError("not ready")
    sb = ProcessSandbox(_policy(), on_worker_start=hook)
    with pytest.raises(KeyError, match="not ready"):
        sb.__enter__()
    assert sb._process is None
    with pytest.raises(RuntimeError, match="Worker process is not running"):
        sb.exec("1")


def test_a_hook_that_raises_on_a_replacement_fails_that_exec_only():
    hook = Recorder()
    with ProcessSandbox(_policy(), on_worker_start=hook) as sb:
        _kill_worker(sb)
        hook.fail = KeyError("not ready")
        with pytest.raises(KeyError, match="not ready"):
            sb.exec(STATE)
        assert sb._process is None
        hook.fail = None
        pid, prepared_in = _state(sb)
        assert prepared_in == pid
    assert hook.generations == [1, 2, 3]


def test_a_setup_that_fails_in_the_worker_fails_the_start():
    hook = Recorder("raise ValueError('bad setup')")
    with pytest.raises(RuntimeError, match="bad setup"):
        with ProcessSandbox(_policy(), on_worker_start=hook):
            pass


def test_the_hook_gives_setup_a_budget_of_its_own():
    hook = Recorder("while True: pass", timeout=0.3)
    with pytest.raises(RuntimeError, match=r"0\.3s timeout"):
        with ProcessSandbox(_policy(timeout=30.0), on_worker_start=hook):
            pass


def test_a_hook_that_returns_with_the_worker_dead_fails_the_start():
    def hook(sb):
        _kill_worker(sb)

    sb = ProcessSandbox(_policy(), on_worker_start=hook)
    with pytest.raises(RuntimeError, match="died while on_worker_start ran"):
        sb.__enter__()


def test_a_worker_that_dies_during_the_hook_is_not_replaced_inside_it():
    calls = []

    def hook(sb):
        calls.append(sb.generation)
        _kill_worker(sb)
        sb.exec("1")  # finds the worker dead; replacing it would recurse

    sb = ProcessSandbox(_policy(), on_worker_start=hook)
    with pytest.raises(RuntimeError, match="died while on_worker_start ran"):
        sb.__enter__()
    assert calls == [1]


def test_a_hook_that_is_not_callable_is_refused():
    with pytest.raises(TypeError, match="must be callable"):
        ProcessSandbox(_policy(), on_worker_start="prepare")


@pytest.mark.parametrize("isolation", ["process", "kernel"])
def test_the_factory_passes_the_hook_on(isolation):
    hook = Recorder()
    try:
        sb = sandbox(_policy(), isolation=isolation, on_worker_start=hook)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")
    try:
        with sb:
            pid, prepared_in = _state(sb)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")
    assert prepared_in == pid
    assert hook.generations == [1]


def test_the_factory_refuses_a_hook_with_no_worker():
    with pytest.raises(ValueError, match="needs a worker"):
        sandbox(_policy(), isolation="none", on_worker_start=Recorder())
