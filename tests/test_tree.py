"""Tests for ``supervise_tree``: every process a worker starts is ended
when the worker exits, and ``drain()`` says when all of them are gone."""

from __future__ import annotations

import multiprocessing.forkserver
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from sandtrap import DrainResult, IsolationUnavailable, Policy, StDeadline, sandbox
from sandtrap.process.sandbox import ProcessSandbox
from sandtrap.process.tree import Tree, _start_time

from . import tree_helpers, tree_import_probe

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="supervise_tree is Linux-only"
)


def _policy(timeout=10.0):
    policy = Policy(timeout=timeout)
    policy.module(tree_helpers, name="tree_helpers")
    return policy


def _supervised(**kwargs):
    # A process boundary without kernel restrictions: seccomp would stop
    # the helpers at execve.
    kwargs.setdefault("tree_grace", 5.0)
    return ProcessSandbox(_policy(), isolation="none", supervise_tree=True, **kwargs)


def _stat(pid):
    """A process's state letter and parent pid, or None once it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):  # gone, or going
        return None
    return fields[0], int(fields[1])


def _running(pid):
    stat = _stat(pid)
    return stat is not None and stat[0] != "Z"


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture
def helpers():
    """Pids of helpers a test started; any still running are killed after
    it, so a failing test leaves nothing behind."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if _running(pid):
            os.kill(pid, signal.SIGKILL)


def _spawn(sb, helpers, *args):
    result = sb.exec(
        f"import tree_helpers\npid = tree_helpers.spawn({', '.join(args)})"
    )
    assert result.error is None, result.error
    pid = result.namespace["pid"]
    helpers.append(pid)
    return pid


def _worker_pid(sb):
    result = sb.exec("import tree_helpers\npid = tree_helpers.me()")
    assert result.error is None, result.error
    return result.namespace["pid"]


# ----------------------------------------------------------------------
# Ending what a worker left behind
# ----------------------------------------------------------------------


@linux_only
def test_drain_confirms_a_helper_ended_by_sigterm(helpers):
    with _supervised() as sb:
        pid = _spawn(sb, helpers, "60")
        assert _running(pid)
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=0)
    assert result.confirmed
    assert not _running(pid)


@linux_only
def test_drain_waits_while_a_helper_finishes_within_the_grace(helpers):
    with _supervised(tree_grace=10.0) as sb:
        pid = _spawn(sb, helpers, "0.6", "ignore_term=True")
        started = time.monotonic()
        result = sb.drain(timeout=10.0)
        waited = time.monotonic() - started
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=0)
    assert waited >= 0.4
    assert not _running(pid)


@linux_only
def test_drain_kills_a_helper_that_outlasts_the_grace(helpers):
    with _supervised(tree_grace=0.2) as sb:
        pid = _spawn(sb, helpers, "60", "ignore_term=True")
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=1)
    assert not _running(pid)


@linux_only
def test_a_detached_helper_is_adopted_and_ended(helpers):
    with _supervised(tree_grace=0.2) as sb:
        pid = _spawn(sb, helpers, "60", "ignore_term=True", "detach=True")
        # Its parent exited at once, so it already belongs to the supervisor.
        assert _wait_until(lambda: _stat(pid)[1] == sb._process.pid)
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=1)
    assert not _running(pid)


@linux_only
def test_a_worker_with_no_helpers_drains_at_once():
    with _supervised() as sb:
        assert sb.exec("x = 1").error is None
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1)


@linux_only
def test_a_sandbox_that_never_started_drains_at_once():
    assert _supervised().drain() == DrainResult("confirmed")


# ----------------------------------------------------------------------
# The broker stays warm; the workers are fresh
# ----------------------------------------------------------------------


@linux_only
def test_successive_sandboxes_share_the_broker_but_not_workers(helpers):
    workers = []
    for _ in range(2):
        with _supervised() as sb:
            if sb._start_method != "forkserver":
                pytest.skip("only forkserver workers have a broker")
            pid = _spawn(sb, helpers, "60")
            supervisor = sb._process.pid
            broker = multiprocessing.forkserver._forkserver._forkserver_pid
            assert _stat(supervisor)[1] == broker
            worker = _worker_pid(sb)
            assert _stat(worker)[1] == supervisor
            workers.append((broker, worker))
            assert sb.drain(timeout=10.0).confirmed
            assert not _running(pid)
    (broker_1, worker_1), (broker_2, worker_2) = workers
    assert broker_1 == broker_2
    assert _running(broker_2)
    assert worker_1 != worker_2


# ----------------------------------------------------------------------
# However the worker goes
# ----------------------------------------------------------------------


@linux_only
def test_a_worker_that_dies_takes_its_helpers_with_it(helpers):
    with _supervised(tree_grace=0.2) as sb:
        pid = _spawn(sb, helpers, "60", "ignore_term=True")
        os.kill(_worker_pid(sb), signal.SIGKILL)
        assert _wait_until(lambda: not sb.worker_alive)
        assert sb._process.exitcode == -signal.SIGKILL
        # Ended by its supervisor, before anyone asks.
        assert _wait_until(lambda: not _running(pid))
        # The next execution gets a fresh worker, and drain covers both.
        assert sb.exec("x = 1").error is None
        assert sb.generation == 2
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=2, helpers=1, killed=1)


@linux_only
def test_a_cancelled_execution_reaches_the_worker_and_drains(helpers):
    with _supervised(tree_grace=0.2) as sb:
        pid = _spawn(sb, helpers, "60")
        done = threading.Event()
        results = []

        def run():
            results.append(sb.exec("while True: pass"))
            done.set()

        thread = threading.Thread(target=run)
        thread.start()
        # A cancel can land before the execution starts and be lost, so it
        # is repeated until the execution ends.
        while not done.wait(0.2):
            sb.cancel()
        thread.join()
        assert results[0].error is not None
        assert sb.worker_alive  # cancelled, not killed
        assert _running(pid)
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=0)
    assert not _running(pid)


@linux_only
def test_a_call_killed_at_its_deadline_returns_once_its_tree_is_gone(helpers):
    policy = _policy()
    policy.module(time)
    with ProcessSandbox(
        policy, isolation="none", supervise_tree=True, tree_grace=0.5
    ) as sb:
        pid = _spawn(sb, helpers, "60", "ignore_term=True")
        started = time.monotonic()
        result = sb.exec("import time\ntime.sleep(30)", deadline=0.3)
        took = time.monotonic() - started
        assert isinstance(result.error, StDeadline)
        # Its helper outlasted SIGTERM and needed the SIGKILL after the
        # grace, and was gone before the call returned.
        assert not _running(pid)
        assert took >= 0.3 + 0.5
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=1)


@linux_only
def test_a_call_killed_at_its_deadline_does_not_wait_out_an_empty_trees_grace():
    policy = _policy()
    policy.module(time)
    with ProcessSandbox(
        policy, isolation="none", supervise_tree=True, tree_grace=5.0
    ) as sb:
        started = time.monotonic()
        result = sb.exec("import time\ntime.sleep(30)", deadline=0.3)
        took = time.monotonic() - started
        assert isinstance(result.error, StDeadline)
        assert took < 0.3 + 3.0
        assert sb.exec("x = 1").namespace["x"] == 1
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=2, helpers=0, killed=0)


@linux_only
def test_a_worker_killed_for_a_failed_start_is_drained(helpers):
    def prepare(sb):
        _spawn(sb, helpers, "60", "ignore_term=True")
        raise RuntimeError("setup failed")

    sb = _supervised(tree_grace=0.2, on_worker_start=prepare)
    with pytest.raises(RuntimeError, match="setup failed"):
        sb.__enter__()
    result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=1)
    assert not _running(helpers[0])


@linux_only
def test_losing_the_supervisor_is_reported(helpers):
    with _supervised() as sb:
        pid = _spawn(sb, helpers, "60")
        worker = _worker_pid(sb)
        os.kill(sb._process.pid, signal.SIGKILL)
        result = sb.drain(timeout=10.0)
        assert result.status == "lost_contact"
        assert not result.confirmed
        # Nothing later recovers it.
        assert sb.drain(timeout=1.0).status == "lost_contact"
        # The dead supervisor's status is collected, not left a zombie.
        (tree,) = sb._trees
        assert tree._supervisor.exitcode == -signal.SIGKILL
    # The worker is stopped rather than left unsupervised; what it started
    # is not accounted for.
    assert _wait_until(lambda: not _running(worker))
    assert _running(pid)


@linux_only
def test_a_worker_that_kills_its_supervisor_is_stopped():
    with _supervised() as sb:
        worker = _worker_pid(sb)
        result = sb.exec("import tree_helpers\ntree_helpers.kill_supervisor()")
        assert result.error is not None  # stopped, not left running
        assert _wait_until(lambda: not _running(worker))
        assert sb.drain().status == "lost_contact"


class _DeadSupervisor:
    pid = None
    exitcode = -signal.SIGKILL

    def join(self, timeout=None):
        pass


class _Report:
    """A report pipe that names a worker, then closes as a dead
    supervisor's does."""

    def __init__(self, *messages):
        self._messages = list(messages)

    def poll(self, timeout=0):
        return True

    def recv(self):
        if self._messages:
            return self._messages.pop(0)
        raise EOFError

    def close(self):
        pass


@linux_only
@pytest.mark.parametrize("same", [True, False], ids=["worker", "reused-pid"])
def test_a_lost_worker_is_killed_only_while_its_pid_names_it(same):
    # The process at the reported pid: the worker itself, or another that
    # took the pid after the worker was reaped, which started later.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        start = _start_time(proc.pid)
        reported = start if same else start - 1
        tree = Tree(_DeadSupervisor(), _Report(("worker", proc.pid, reported)))
        tree.wait_drained(timeout=1.0)
        assert tree.lost
        if same:
            assert _wait_until(lambda: not _running(proc.pid))
        else:
            time.sleep(0.2)
            assert _running(proc.pid)
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_a_drain_that_runs_out_of_time_can_be_resumed(helpers):
    with _supervised(tree_grace=10.0) as sb:
        pid = _spawn(sb, helpers, "1.0", "ignore_term=True")
        assert sb.drain(timeout=0.1).status == "timed_out"
        assert _running(pid)
        result = sb.drain(timeout=10.0)
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=0)


@linux_only
def test_a_drain_without_waiting_takes_in_what_has_arrived():
    with _supervised() as sb:
        assert sb.exec("x = 1").error is None
    # Every report is in by now; a zero wait must still read them all.
    assert _wait_until(lambda: not _running(sb._trees[0].pid))
    assert sb.drain(timeout=0) == DrainResult("confirmed", workers=1)


@linux_only
def test_settled_trees_are_let_go_as_workers_are_replaced():
    with _supervised() as sb:
        for _ in range(3):
            pid = _worker_pid(sb)
            os.kill(pid, signal.SIGKILL)
            assert _wait_until(lambda: not sb.worker_alive)
            supervisor = sb._process.pid
            assert _wait_until(lambda: not _running(supervisor))
        assert sb.exec("x = 1").error is None
        # Only the running worker's tree is still tracked.
        assert len(sb._trees) == 1
        assert sb.drain() == DrainResult("confirmed", workers=4)


@linux_only
def test_a_drained_sandbox_starts_no_worker():
    sb = _supervised()
    with sb:
        assert sb.drain().confirmed
        with pytest.raises(RuntimeError, match="drained"):
            sb.exec("x = 1")
    with pytest.raises(RuntimeError, match="drained"):
        sb.__enter__()


@linux_only
def test_drain_inside_and_after_the_block_agree():
    sb = _supervised()
    with sb:
        assert sb.exec("x = 1").error is None
    # shutdown() already stopped the worker; drain still confirms its tree.
    assert sb.drain() == DrainResult("confirmed", workers=1)


@linux_only
def test_grants_are_imported_by_the_worker_not_its_supervisor():
    """A grant's import-time code runs in the process that imports it, and
    a supervisor that imported one would fork the worker from that state."""
    policy = _policy()
    policy.module(tree_import_probe, name="probe")
    with ProcessSandbox(policy, isolation="none", supervise_tree=True) as sb:
        if sb._start_method == "fork":
            pytest.skip("a forked worker inherits this process's imports")
        result = sb.exec("import probe\nwhere = probe.where()")
        assert result.error is None, result.error
        sb.drain()
    worker, imported_in = result.namespace["where"]
    assert imported_in == worker


@linux_only
def test_kernel_isolation_can_be_supervised():
    with sandbox(
        _policy(), isolation="kernel", supervise_tree=True, allow_degraded=True
    ) as sb:
        assert sb.exec("x = 1").error is None
        assert sb.drain() == DrainResult("confirmed", workers=1)


# ----------------------------------------------------------------------
# What a sandbox reports about its worker
# ----------------------------------------------------------------------


@pytest.mark.parametrize("supervise", [False, pytest.param(True, marks=linux_only)])
def test_worker_alive_answers_for_the_worker(supervise):
    with ProcessSandbox(_policy(), isolation="none", supervise_tree=supervise) as sb:
        assert sb.worker_alive
        os.kill(_worker_pid(sb), signal.SIGKILL)
        assert _wait_until(lambda: not sb.worker_alive)
        assert sb.exec("x = 1").error is None
        assert sb.worker_alive
    assert not sb.worker_alive


# ----------------------------------------------------------------------
# Where it can't be asked for
# ----------------------------------------------------------------------


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="Linux supports it")
def test_supervise_tree_is_refused_off_linux():
    with pytest.raises(IsolationUnavailable, match="needs Linux"):
        ProcessSandbox(_policy(), supervise_tree=True)


def test_drain_needs_supervise_tree():
    with pytest.raises(RuntimeError, match="supervise_tree=True"):
        ProcessSandbox(_policy(), supervise_tree=False).drain()


def test_the_factory_refuses_supervise_tree_in_process():
    with pytest.raises(ValueError, match="supervise_tree needs a worker"):
        sandbox(_policy(), isolation="none", supervise_tree=True)


@pytest.mark.parametrize(
    ("grace", "error"), [(-1, ValueError), (float("inf"), ValueError), ("1", TypeError)]
)
def test_tree_grace_must_be_a_duration(grace, error):
    with pytest.raises(error):
        ProcessSandbox(_policy(), tree_grace=grace)
