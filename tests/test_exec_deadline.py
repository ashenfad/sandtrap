"""Tests for ``deadline`` on ``exec`` and ``aexec``: a hard limit on one
execution, kept by the parent, which kills the worker."""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from sandtrap import (
    IsolationUnavailable,
    Policy,
    RpcProxyMarker,
    StDeadline,
    StTimeout,
    sandbox,
)

ISOLATIONS = ("process", "kernel")
RUNNERS = ("exec", "aexec")

# Killing and reaping a worker, on a loaded CI machine.
SLACK = 3.0


class Slow:
    """A host-time registration: its calls run in the worker, off the
    execution's clock."""

    def wait(self, seconds: float) -> str:
        time.sleep(seconds)
        return "done"


def _sandbox(isolation, **kwargs):
    policy = Policy(timeout=30.0)
    policy.module(time)
    policy.cls(Slow, name="Slow", host_time=True)
    try:
        return sandbox(policy, isolation=isolation, **kwargs)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


def _run(sb, runner, source, **kwargs):
    if runner == "aexec":
        return asyncio.run(sb.aexec(source, **kwargs))
    return sb.exec(source, **kwargs)


def _timed(sb, runner, source, **kwargs):
    start = time.monotonic()
    result = _run(sb, runner, source, **kwargs)
    return result, time.monotonic() - start


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_deadline_ends_code_blocked_in_a_call(isolation, runner):
    """The timeout can't stop a call that never returns to the code; the
    deadline doesn't wait for it to."""
    with _sandbox(isolation) as sb:
        result, took = _timed(
            sb, runner, "import time\ntime.sleep(30)\nok = True", deadline=0.5
        )
    assert isinstance(result.error, StDeadline)
    assert isinstance(result.error, StTimeout)
    assert "0.5s deadline" in str(result.error)
    assert "ok" not in result.namespace
    assert took < 0.5 + SLACK


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_deadline_does_not_stop_for_host_time(isolation):
    with _sandbox(isolation) as sb:
        result, took = _timed(
            sb, "exec", "h.wait(30)\nok = True", namespace={"h": Slow()}, deadline=0.5
        )
    assert isinstance(result.error, StDeadline)
    assert took < 0.5 + SLACK


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_result_after_an_rpc_past_the_deadline_is_not_a_success(isolation):
    """An RPC handler runs in the parent's wait, so the deadline can't land
    during one; it lands as soon as the handler returns, and the result the
    code would have made doesn't come back."""

    def slow(method, args, kwargs):
        time.sleep(1.0)
        return "done"

    with _sandbox(isolation, rpc_handlers={"slow": slow}) as sb:
        result, took = _timed(
            sb,
            "exec",
            "slow.wait()\nok = True",
            namespace={"slow": RpcProxyMarker(target="slow")},
            deadline=0.3,
        )
    assert isinstance(result.error, StDeadline)
    assert "ok" not in result.namespace
    assert took >= 1.0


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_execution_within_its_deadline_keeps_its_worker(isolation, runner):
    with _sandbox(isolation) as sb:
        result = _run(sb, runner, "x = 1", deadline=10.0)
        assert result.error is None, result.error
        assert result.namespace["x"] == 1
        generation = sb.generation
        assert _run(sb, runner, "y = 2").namespace["y"] == 2
        assert sb.generation == generation


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_the_next_execution_after_a_deadline_starts_a_fresh_worker(isolation):
    with _sandbox(isolation) as sb:
        generation = sb.generation
        result = sb.exec("import time\ntime.sleep(30)", deadline=0.3)
        assert isinstance(result.error, StDeadline)
        assert not sb.worker_alive
        result = sb.exec("y = 2")
        assert result.error is None, result.error
        assert result.namespace["y"] == 2
        assert sb.generation == generation + 1


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_the_timeout_still_stops_code_that_runs(isolation):
    """Code that reaches checkpoints is stopped by its timeout, as before,
    and keeps its worker."""
    with _sandbox(isolation) as sb:
        generation = sb.generation
        result = sb.exec("while True: pass", timeout=0.3, deadline=10.0)
        assert isinstance(result.error, StTimeout)
        assert not isinstance(result.error, StDeadline)
        assert sb.worker_alive
        assert sb.generation == generation


@pytest.mark.parametrize(
    "bad, error",
    [
        (0, ValueError),
        (-1.0, ValueError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (True, TypeError),
        ("1", TypeError),
    ],
)
@pytest.mark.parametrize("runner", RUNNERS)
def test_a_deadline_that_is_not_a_positive_finite_number_is_refused(runner, bad, error):
    with _sandbox("process") as sb:
        with pytest.raises(error, match="deadline"):
            _run(sb, runner, "1", deadline=bad)


@pytest.mark.parametrize("runner", RUNNERS)
def test_an_in_process_sandbox_refuses_a_deadline(runner):
    """It has no worker to kill, and pretending to keep a deadline it
    can't enforce would be worse than saying so."""
    with _sandbox("none") as sb:
        with pytest.raises(ValueError, match="deadline needs a worker"):
            _run(sb, runner, "x = 1", deadline=1.0)
        assert _run(sb, runner, "x = 1", deadline=None).namespace["x"] == 1
