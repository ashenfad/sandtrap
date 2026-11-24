"""Supervising the processes a worker starts (Linux).

A supervised worker is not the process ``multiprocessing`` starts. That
process, the supervisor, marks itself a child subreaper and forks the
worker. Whatever the worker starts, however it detaches (a double fork,
``setsid``), is reparented to the supervisor when its own parent exits,
so the supervisor sees the worker's whole tree. When the worker exits it
ends the rest of the tree, waits until it has no children left, says so
over a pipe of its own, and exits.

The supervisor is forked from the same place the worker would have been
(the forkserver broker by default), so the worker still inherits the
broker's preloaded modules, and the broker never learns of anything but
the supervisor.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..sandbox import IsolationUnavailable

# prctl option that makes the caller a child subreaper: orphaned
# descendants are reparented to it rather than to init.
_PR_SET_CHILD_SUBREAPER = 36

# Longest pause between rounds while a tree is being ended.
_ROUND_PAUSE = 0.05

# Most reports taken in at once without waiting. A supervisor sends three
# in all, so this bounds the reading without ever cutting it short.
_PENDING_REPORTS = 4


@dataclass(frozen=True)
class DrainResult:
    """What :meth:`ProcessSandbox.drain` could confirm about the processes
    its workers started.

    ``status`` is ``"confirmed"`` when every worker the sandbox started has
    exited and everything each one left behind is gone. ``"timed_out"``
    means some worker's tree was still being ended when the wait ran out;
    drain again to wait longer. ``"lost_contact"`` means a worker's
    supervisor exited without confirming its tree, so what that worker
    left can no longer be accounted for, and nothing later will change
    that.

    ``workers`` counts the workers whose trees are confirmed gone.
    ``helpers`` counts the processes those workers left running when they
    exited, and ``killed`` how many of those were still running when the
    grace period ran out and had to be killed.
    """

    status: Literal["confirmed", "timed_out", "lost_contact"]
    workers: int = 0
    helpers: int = 0
    killed: int = 0

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


def require_supervision() -> None:
    """Refuse, at construction, a platform that can't supervise a tree."""
    if not sys.platform.startswith("linux"):
        raise IsolationUnavailable(
            "supervise_tree needs Linux: it accounts for every process a "
            "worker starts through a child subreaper "
            f"(prctl PR_SET_CHILD_SUBREAPER), and sys.platform is {sys.platform!r}"
        )


# ----------------------------------------------------------------------
# The parent's view
# ----------------------------------------------------------------------


class Tree:
    """A supervised worker, as its sandbox sees it.

    It stands where the worker's ``multiprocessing.Process`` would:
    ``is_alive``, ``join``, ``exitcode`` and ``kill`` answer for the worker
    itself, from its supervisor's reports. ``pid`` is the supervisor's, and
    a ``SIGUSR1`` sent there reaches the worker. The supervisor outlives
    the worker until the rest of the tree is gone, and :meth:`wait_drained`
    waits for its last report.
    """

    def __init__(self, supervisor: Any, report: Any) -> None:
        self._supervisor = supervisor
        self._report = report
        # One reader at a time: cancel() asks is_alive() from any thread
        # while an execution's wait may be reading.
        self._lock = threading.Lock()
        self.worker_pid: int | None = None
        # The worker's start time, with its pid, names it for good: the pid
        # alone may be reused once the worker is reaped.
        self._worker_start: int | None = None
        self.exitcode: int | None = None
        self.worker_exited = False
        self.drained = False
        self.lost = False
        self.helpers = 0
        self.killed = 0

    @property
    def pid(self) -> int | None:
        return self._supervisor.pid

    def is_alive(self) -> bool:
        """Whether the worker may still be running. Takes in every report
        already sent, the last one included, without waiting."""
        if self._lock.acquire(blocking=False):
            try:
                self._take_pending(lambda: self.drained or self.lost)
            finally:
                self._lock.release()
        return not self._worker_done()

    def join(self, timeout: float | None = None) -> None:
        """Wait up to ``timeout`` seconds for the worker to exit."""
        self._wait(self._worker_done, timeout)

    def kill(self) -> None:
        """Have the supervisor kill the worker. The supervisor then ends
        the rest of the tree as it does after any worker exit."""
        if self._worker_done():
            return
        try:
            os.kill(self._supervisor.pid, signal.SIGTERM)
        except OSError:
            pass

    def wait_drained(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the supervisor to confirm the
        tree is gone, or for contact with it to be lost."""
        self._wait(lambda: self.drained or self.lost, timeout)
        if self.drained:
            # It exits right after its last report; collect its status.
            self._supervisor.join(timeout=1.0)

    def _worker_done(self) -> bool:
        return self.worker_exited or self.lost

    def _wait(self, done: Any, timeout: float | None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not done() and self._report is not None:
            remaining = 0.1 if deadline is None else deadline - time.monotonic()
            with self._lock:
                # Everything already sent first, so a wait that has run out
                # still answers with what has arrived.
                self._take_pending(done)
                if done() or remaining <= 0:
                    return
                self._receive(min(0.1, remaining))

    def _take_pending(self, done: Any) -> None:
        """Take in the reports already sent, until ``done``. The caller
        holds the lock."""
        for _ in range(_PENDING_REPORTS):
            if done() or not self._receive(0):
                return

    def _receive(self, timeout: float) -> bool:
        """Take in one report, waiting up to ``timeout`` seconds for it;
        whether one arrived. The caller holds the lock."""
        report = self._report
        if report is None:
            return False
        try:
            if not report.poll(timeout):
                return False
            message = report.recv()
        except (EOFError, OSError):
            self._lose()
            return True
        if not isinstance(message, tuple) or not message:
            return True  # not a report; nothing to take from it
        kind = message[0]
        if kind == "worker":
            self.worker_pid, self._worker_start = message[1], message[2]
        elif kind == "exit":
            self.exitcode = message[1]
            self.worker_exited = True
        elif kind == "drained":
            self.helpers, self.killed = message[1], message[2]
            self.drained = True
            self._close()
        return True

    def _lose(self) -> None:
        """The supervisor is gone without confirming the tree."""
        self.lost = True
        if self.worker_pid is not None and not self.worker_exited:
            # Nothing supervises the worker now. Stop it rather than leave
            # it running unaccounted for; what it started stays unknown.
            # Nothing holds its pid any more either, so it may have exited
            # and been reaped, and another process may hold that pid now.
            _kill_if_same(self.worker_pid, self._worker_start)
        # Its pipe closing means it has exited: collect its status, so it
        # isn't left a zombie of this process.
        self._supervisor.join(timeout=1.0)
        if self.worker_pid is None:
            # It died before naming a worker, so it was all there was of
            # this one: its exit status is the one to describe.
            self.exitcode = self._supervisor.exitcode
        self._close()

    def _close(self) -> None:
        if self._report is not None:
            try:
                self._report.close()
            except OSError:
                pass
            self._report = None


def _start_time(pid: int) -> int | None:
    """When process ``pid`` started, in clock ticks since boot, or None if
    there is no such process. A pid and its start time name one process
    for good."""
    from monkeyfs import suspend

    try:
        with suspend(), open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return None
    # The fields after the command name's last ')' start at the state, the
    # third; the start time is the twenty-second.
    return int(stat[stat.rfind(b")") + 2 :].split()[19])


def _kill_if_same(pid: int, start: int | None) -> None:
    """SIGKILL process ``pid`` only if it is still the one that started at
    ``start``. The pidfd is taken before the check, so the process checked
    is the one signalled. A kernel without pidfds (before 5.3) gets no
    signal: a process that can't be told apart is left alone."""
    if start is None:
        return
    try:
        pidfd = os.pidfd_open(pid)
    except OSError:
        return  # gone already, or no pidfds here
    try:
        if _start_time(pid) == start:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(pidfd)


# ----------------------------------------------------------------------
# The supervisor
# ----------------------------------------------------------------------


def tree_entry(
    report: Any,
    grace: float,
    conn: Any,
    parent_connections: tuple[Any, ...],
    inherited_fds: tuple[int, ...],
    worker_state: Any,
) -> None:
    """Target of a supervised worker's process: fork the worker from here,
    then supervise its tree.

    ``worker_state`` is the rest of the worker's arguments, the policy
    first: the tuple itself, or its pickle, which only the worker loads so
    that nothing it imports is imported here first.

    Both sides return through the caller's bootstrap, so the worker exits
    exactly as an unsupervised one does, with the same exit status.
    """
    import pickle
    import traceback

    from .protocol import WorkerErrorMsg
    from .sandbox import _neutralize_inherited_fds, _worker_entry

    worker: dict[str, Any] = {"pid": None, "running": False}

    def forward_cancel(signum: int, frame: Any) -> None:
        if worker["running"]:
            try:
                os.kill(worker["pid"], signal.SIGUSR1)
            except OSError:
                pass

    def kill_worker(signum: int, frame: Any) -> None:
        if worker["running"]:
            try:
                os.kill(worker["pid"], signal.SIGKILL)
            except OSError:
                pass

    def fail(message: str) -> None:
        try:
            conn.send(WorkerErrorMsg(message=message))
        except OSError:
            pass
        _send(report, ("exit", 1))
        _send(report, ("drained", 0, 0))

    try:
        _become_subreaper()
    except OSError as exc:
        fail(f"could not supervise the worker's process tree: {exc}")
        return
    # The supervisor gives up the host's descriptors before the worker
    # exists, so neither keeps one; the worker repeats it, harmlessly.
    try:
        _neutralize_inherited_fds(inherited_fds)
    except BaseException:
        fail(traceback.format_exc())
        return

    # Handlers go in before the fork, and the signals they serve stay
    # blocked across it, so none is acted on before the supervisor knows
    # the worker's pid. Ctrl-C at a terminal reaches the whole process
    # group, and the supervisor must outlive the worker, so it ignores it.
    handlers = {
        signal.SIGUSR1: forward_cancel,
        signal.SIGTERM: kill_worker,
        signal.SIGINT: signal.SIG_IGN,
        # Children must stay waitable: SIG_IGN would reap them unseen.
        signal.SIGCHLD: signal.SIG_DFL,
    }
    originals = {sig: signal.signal(sig, handler) for sig, handler in handlers.items()}
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1, signal.SIGTERM})
    # The worker waits at this gate until the parent has its pid, so no
    # code of the worker's runs while the supervisor is the only one who
    # could stop it.
    gate_closed, gate_open = os.pipe()
    try:
        pid = os.fork()
    except OSError as exc:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        os.close(gate_closed)
        os.close(gate_open)
        fail(f"could not start the worker: {exc}")
        return

    if pid == 0:
        for sig, handler in originals.items():
            signal.signal(sig, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        report.close()
        os.close(gate_open)
        try:
            opened = os.read(gate_closed, 1)
        finally:
            os.close(gate_closed)
        if not opened:
            return  # the supervisor died first: nothing would supervise this
        try:
            if isinstance(worker_state, bytes):
                worker_state = pickle.loads(worker_state)
            (
                policy,
                filesystem,
                mode,
                isolation,
                snapshot_prints,
                echo,
                _,
                spool_dir,
                render_prints,
            ) = worker_state
        except BaseException:
            try:
                conn.send(WorkerErrorMsg(message=traceback.format_exc()))
            except BaseException:
                pass  # the parent sees EOF and describes the exit
            return
        _worker_entry(
            conn,
            parent_connections,
            policy,
            filesystem,
            mode,
            isolation,
            snapshot_prints,
            echo,
            inherited_fds,
            spool_dir,
            render_prints,
        )
        return

    worker["pid"] = pid
    worker["running"] = True
    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    os.close(gate_closed)
    # Read while the worker can't have been reaped, since only the
    # supervisor reaps it, so the start time is the worker's own.
    _send(report, ("worker", pid, _start_time(pid)))
    try:
        os.write(gate_open, b"\0")
    except OSError:
        pass  # the worker is gone already; the wait below reaps it
    finally:
        os.close(gate_open)

    # The supervisor holds none of the worker's channels: the parent learns
    # of the worker's death from EOF on its control pipe, which a copy here
    # would hold open.
    conn.close()
    for parent_conn in parent_connections:
        try:
            parent_conn.close()
        except OSError:
            pass

    _send(report, ("exit", _wait_for_worker(worker)))
    helpers, killed = _end_tree(grace)
    _send(report, ("drained", helpers, killed))


def _become_subreaper() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(
            ctypes.c_int(_PR_SET_CHILD_SUBREAPER),
            ctypes.c_ulong(1),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
        != 0
    ):
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _send(report: Any, message: tuple) -> None:
    try:
        report.send(message)
    except OSError:
        pass  # the parent is gone; nobody is left to tell


def _wait_for_worker(worker: dict[str, Any]) -> int:
    """Reap children until the worker is among them; its exit code.

    Descendants orphaned while the worker runs are reaped as they exit.
    Each exited child is seen before it is reaped, so a signal handler
    that runs in between still finds the worker's pid held by its zombie
    and can never signal a process that reused it.
    """
    while True:
        info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOWAIT)
        if info is None:
            continue
        if info.si_pid == worker["pid"]:
            worker["running"] = False
        _, status = os.waitpid(info.si_pid, 0)
        if not worker["running"]:
            return os.waitstatus_to_exitcode(status)


def _end_tree(grace: float) -> tuple[int, int]:
    """End every process left under the supervisor; how many there were,
    and how many had to be killed.

    Each is sent SIGTERM and given ``grace`` seconds to exit, then
    SIGKILL. Rounds repeat until the supervisor has no children at all:
    a process killed in one round hands its own children to the
    supervisor for the next, and anything started meanwhile is caught
    there too. No children means no descendants, since every descendant
    whose parent exits is reparented here.
    """
    seen: set[int] = set()
    termed: set[int] = set()
    killed: set[int] = set()
    deadline = time.monotonic() + grace
    pause = 0.005
    while _reap_children():
        tree = _descendants()
        seen |= tree
        if time.monotonic() < deadline:
            sig, sent = signal.SIGTERM, termed
        else:
            sig, sent = signal.SIGKILL, killed
        for pid in tree - sent:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            sent.add(pid)
        time.sleep(pause)
        pause = min(pause * 2, _ROUND_PAUSE)
    return len(seen), len(killed)


def _reap_children() -> bool:
    """Reap every child that has exited; whether any children remain."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        if pid == 0:
            return True


def _descendants() -> set[int]:
    """Every live process below this one, read from ``/proc``.

    Zombies are left out: one has already exited, and its children were
    reparented when it did.
    """
    from monkeyfs import suspend

    children: dict[int, list[int]] = {}
    with suspend():
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    stat = f.read()
            except OSError:
                continue
            # The command name is parenthesized and may itself hold spaces
            # or parentheses; the fields after its last ')' are fixed.
            fields = stat[stat.rfind(b")") + 2 :].split()
            if fields[0] == b"Z":
                continue
            children.setdefault(int(fields[1]), []).append(int(name))
    found: set[int] = set()
    stack = [os.getpid()]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found
