"""Tests for ``deadline`` on ``exec`` and ``aexec``: a hard limit on one
execution, kept by the parent, which kills the worker."""

from __future__ import annotations

import asyncio
import math
import multiprocessing.connection
import os
import threading
import time
from unittest.mock import patch

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

# How long a stalled receive waits for more of its frame when nothing
# ends it: far past any deadline these tests set.
STALL = 10.0


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


def _patch_recv(sb, after):
    """Patch ``Connection.recv`` so that, on ``sb``'s connection only, each
    message received goes through ``after(conn, msg)`` before it is
    returned."""
    conn = sb._conn
    real_recv = multiprocessing.connection.Connection.recv

    def recv(self):
        msg = real_recv(self)
        return after(self, msg) if self is conn else msg

    return patch.object(multiprocessing.connection.Connection, "recv", recv)


def _watchdogs():
    return [t for t in threading.enumerate() if t.name == "sandtrap-deadline"]


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


def _received_late(conn, msg):
    time.sleep(1.0)  # the receive returns its message past the deadline
    return msg


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_result_received_past_the_deadline_is_not_a_success(isolation):
    """A receive under way reads its frame to the end, however long that
    takes; a result whose receipt ends past the deadline doesn't come back
    as a success."""
    with _sandbox(isolation) as sb:
        with _patch_recv(sb, _received_late):
            result, took = _timed(sb, "exec", "ok = True", deadline=0.3)
        assert not sb.worker_alive
    assert isinstance(result.error, StDeadline)
    assert "ok" not in result.namespace
    assert took < 1.0 + SLACK


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_spooled_result_received_past_the_deadline_leaves_no_files(isolation):
    """A late result is discarded, and so are the files it spooled, which
    nothing would read before the sandbox closes."""
    with _sandbox(isolation, spool=True) as sb:
        with _patch_recv(sb, _received_late):
            result = sb.exec("ok = bytes(200_000)", deadline=0.3)
        assert isinstance(result.error, StDeadline)
        assert os.listdir(sb._spool_dir) == []


def _stalled(conn, msg):
    # The rest of the frame never comes: the receive waits on the pipe until
    # the worker writes again, which it doesn't, or its end closes.
    if conn.poll(STALL):
        raise EOFError  # readable with nothing sent: the worker is gone
    return msg


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_stalled_receive_ends_at_the_deadline(isolation):
    """A receive whose frame stops arriving would wait for as long as the
    worker lives; at the deadline the worker is killed, which ends it."""
    with _sandbox(isolation) as sb:
        with _patch_recv(sb, _stalled):
            result, took = _timed(sb, "exec", "ok = True", deadline=0.5)
        assert not sb.worker_alive
    assert isinstance(result.error, StDeadline)
    assert "ok" not in result.namespace
    assert took < 0.5 + SLACK
    assert _watchdogs() == []


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_execution_within_its_deadline_keeps_its_worker(isolation, runner):
    with _sandbox(isolation) as sb:
        result = _run(sb, runner, "x = 1", deadline=10.0)
        assert result.error is None, result.error
        assert result.namespace["x"] == 1
        assert _watchdogs() == []  # stopped with the call, not at its deadline
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


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_deadline_during_a_shutdown_from_another_thread(isolation):
    """A shutdown waiting on the worker while the execution's deadline
    kills it and clears the sandbox's fields finishes without an error."""
    sb = _sandbox(isolation)
    sb.__enter__()
    cleaned = threading.Event()
    real_cleanup = sb._cleanup

    def cleanup(conn=None):
        real_cleanup(conn)
        if threading.current_thread() is not threading.main_thread():
            cleaned.set()

    class Held:
        """The worker, whose wait in the shutdown ends only once the
        execution has cleaned up after its deadline."""

        def __init__(self, process):
            self._process = process

        def __getattr__(self, name):
            return getattr(self._process, name)

        def join(self, timeout=None):
            self._process.join(timeout)
            if threading.current_thread() is threading.main_thread():
                cleaned.wait(10)

    sb._cleanup = cleanup
    sb._process = Held(sb._process)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            sb.exec("import time\ntime.sleep(30)", deadline=0.5)
        )
    )
    thread.start()
    time.sleep(0.2)  # the worker is in the call
    sb.shutdown()
    thread.join(timeout=30)
    assert cleaned.is_set()
    assert not thread.is_alive()
    assert isinstance(results[0].error, StDeadline)
    assert not sb.worker_alive


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
