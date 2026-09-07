"""Tests for ProcessSandbox — subprocess-backed execution."""

import multiprocessing
import os
import signal
import socket
import stat
import threading
import time
import warnings
from unittest.mock import patch

import pytest
from monkeyfs import IsolatedFS, VirtualFS, current_fs

from sandtrap import Policy
from sandtrap.process.protocol import ExecMsg, filter_namespace
from sandtrap.process.sandbox import ProcessSandbox


def _fd_signature(fd):
    """Return enough identity to recognize an inherited open descriptor."""
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino


def _start_worker_then_abandon_parent(report):
    """Start a worker and exit without running Python cleanup handlers."""
    sb = ProcessSandbox(
        Policy(timeout=10.0),
        isolation="none",
    )
    sb.__enter__()
    report.send(sb._process.pid)
    report.close()
    os._exit(0)


def _block_after_notifying_parent():
    """Tell the embedding process execution started, then remain busy."""
    os.kill(os.getppid(), signal.SIGUSR1)
    while True:
        time.sleep(1.0)


def _start_two_workers_then_abandon_parent(report):
    """Leave one worker idle while a later worker remains busy."""
    second_started = False

    def mark_second_started(_signum, _frame):
        nonlocal second_started
        second_started = True

    signal.signal(signal.SIGUSR1, mark_second_started)

    first = ProcessSandbox(Policy(timeout=60.0), isolation="none")
    second_policy = Policy(timeout=60.0)
    second_policy.fn(_block_after_notifying_parent, name="block")
    second = ProcessSandbox(second_policy, isolation="none")
    first.__enter__()
    second.__enter__()

    second._conn.send(ExecMsg(source="block()", namespace=None))
    deadline = time.monotonic() + 5.0
    while not second_started and time.monotonic() < deadline:
        time.sleep(0.01)

    report.send((first._process.pid, second._process.pid, second_started))
    report.close()
    os._exit(0)


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


@pytest.fixture
def psandbox(root):
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        yield ps


@pytest.fixture
def wrapped_psandbox(root):
    """A worker in wrapped mode, for the tests about wrapper reactivation.

    Raw mode returns plain functions and classes, which do not survive
    the worker's namespace filter; only the wrappers cross.
    """
    with ProcessSandbox(
        Policy(timeout=10.0), filesystem=IsolatedFS(root), mode="wrapped"
    ) as ps:
        yield ps


# ------------------------------------------------------------------
# Basic execution
# ------------------------------------------------------------------


def test_simple_arithmetic(psandbox):
    result = psandbox.exec("x = 2 + 3")
    assert result.error is None
    assert result.namespace["x"] == 5


def test_print_capture(psandbox):
    result = psandbox.exec("print('hello')")
    assert result.error is None
    assert result.stdout == "hello\n"


def test_multiple_execs_reuse_worker(psandbox):
    """Worker persists across multiple exec() calls."""
    r1 = psandbox.exec("x = 1")
    r2 = psandbox.exec("y = 2")
    assert r1.error is None
    assert r2.error is None
    assert r1.namespace["x"] == 1
    assert r2.namespace["y"] == 2


def test_namespace_injection(psandbox):
    result = psandbox.exec("y = x + 1", namespace={"x": 10})
    assert result.error is None
    assert result.namespace["y"] == 11


def test_syntax_error(psandbox):
    result = psandbox.exec("def")
    assert result.error is not None
    assert isinstance(result.error, SyntaxError)


def test_runtime_error(psandbox):
    result = psandbox.exec("x = 1 / 0")
    assert result.error is not None
    assert isinstance(result.error, ZeroDivisionError)


def test_runtime_error_carries_rendered_traceback(psandbox):
    """Traceback objects don't survive the pickle back to the parent,
    so the worker renders the frames it alone can see and rides the
    text across on the exception. Line numbers are the payload: an
    agent fixing 'line 3' needs to know it WAS line 3."""
    result = psandbox.exec("a = 1\nb = 2\nc = undefined_name\n")
    text = getattr(result.error, "_st_traceback_text", None)
    assert isinstance(text, str)
    assert "Traceback (most recent call last)" in text
    assert "line 3" in text
    assert "NameError" in text


def test_unpicklable_exception_degrades_to_a_stand_in(psandbox):
    """An exception carrying unpicklable baggage used to kill the send
    and bury the agent's error under worker-crash noise. The stand-in
    keeps the story: class name, message, and the rendered frames."""
    result = psandbox.exec(
        "e = ValueError('boom with baggage')\n"
        "e.baggage = open('/f.txt', 'w')\n"
        "raise e\n"
    )
    assert isinstance(result.error, RuntimeError)
    assert "ValueError: boom with baggage" in str(result.error)
    text = getattr(result.error, "_st_traceback_text", None)
    assert isinstance(text, str)
    assert "line 3" in text and "ValueError" in text
    # and the worker survived the episode
    ok = psandbox.exec("x = 40 + 2")
    assert ok.error is None and ok.namespace["x"] == 42


def test_error_deep_in_a_call_chain_names_every_frame(psandbox):
    result = psandbox.exec(
        "def inner():\n"
        "    raise ValueError('boom')\n"
        "def outer():\n"
        "    inner()\n"
        "outer()\n"
    )
    text = getattr(result.error, "_st_traceback_text", None)
    assert isinstance(text, str)
    assert "in outer" in text and "in inner" in text
    assert "line 2" in text  # the raise site survives the pipe


# ------------------------------------------------------------------
# Timeout
# ------------------------------------------------------------------


def test_timeout_enforcement(root):
    with ProcessSandbox(Policy(timeout=1.0), filesystem=IsolatedFS(root)) as ps:
        result = ps.exec("while True: pass")
        assert result.error is not None
        assert "timeout" in str(result.error).lower()


# ------------------------------------------------------------------
# Cancel
# ------------------------------------------------------------------


def test_cancel(root):
    # Short timeout so the test finishes quickly even if cancel() doesn't
    # work (e.g. SIGUSR1 not handled in forked children on macOS 3.13+).
    with ProcessSandbox(Policy(timeout=5.0), filesystem=IsolatedFS(root)) as ps:
        results = [None]

        def run():
            results[0] = ps.exec("while True: pass")

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        ps.cancel()
        t.join(timeout=15.0)
        assert results[0] is not None
        assert results[0].error is not None


# ------------------------------------------------------------------
# Filesystem via IsolatedFS
# ------------------------------------------------------------------


def test_file_io_within_root(root):
    with open(os.path.join(root, "data.txt"), "w") as f:
        f.write("hello")

    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        result = ps.exec("f = open('/data.txt', 'r')\ncontent = f.read()\nf.close()")
        assert result.error is None
        assert result.namespace["content"] == "hello"


def test_file_write_within_root(root):
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        result = ps.exec("f = open('/output.txt', 'w')\nf.write('written')\nf.close()")
        assert result.error is None

    with open(os.path.join(root, "output.txt")) as f:
        assert f.read() == "written"


# ------------------------------------------------------------------
# Non-picklable namespace values
# ------------------------------------------------------------------


def test_non_picklable_namespace_skipped(psandbox):
    """Non-picklable values are silently dropped from namespace."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = psandbox.exec("y = 42", namespace={"fn": lambda: None, "x": 1})
    assert result.error is None
    assert result.namespace["y"] == 42


# ------------------------------------------------------------------
# Shutdown / cleanup
# ------------------------------------------------------------------


def test_context_manager_cleanup(root):
    ps = ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root))
    with ps:
        ps.exec("x = 1")
    assert ps._process is None


def test_shutdown_without_exec(root):
    """Shutdown before any exec() should not error."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)):
        pass


# ------------------------------------------------------------------
# Isolation mode
# ------------------------------------------------------------------


def test_isolation_none(root):
    """isolation='none' still works (just skips kernel sandboxing)."""
    fs = IsolatedFS(root)
    with ProcessSandbox(Policy(timeout=10.0), filesystem=fs, isolation="none") as ps:
        result = ps.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


@pytest.mark.parametrize(
    "isolation",
    [
        pytest.param("none", id="process"),
        pytest.param("auto", id="kernel"),
    ],
)
def test_worker_does_not_inherit_unrelated_host_socket(isolation):
    """Forking the worker must not carry ambient host capabilities across.

    ``socketpair()`` is intentionally created before the sandbox. A raw fork
    duplicates it into the worker at the same descriptor number with the same
    inode, even though the descriptor is non-inheritable across exec.
    """
    host, peer = socket.socketpair()
    try:
        fd = host.fileno()
        host_signature = _fd_signature(fd)
        policy = Policy(timeout=10.0)
        policy.fn(_fd_signature, name="fd_signature")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with ProcessSandbox(
                policy,
                isolation=isolation,
                allow_degraded=True,
                close_fds=True,
            ) as ps:
                result = ps.exec(
                    "worker_signature = fd_signature(fd)",
                    namespace={"fd": fd},
                )

        assert result.error is None
        assert result.namespace["worker_signature"] != host_signature
    finally:
        host.close()
        peer.close()


@pytest.mark.parametrize(
    "isolation",
    [
        pytest.param("none", id="process"),
        pytest.param("auto", id="kernel"),
    ],
)
def test_worker_does_not_suppress_eof_on_unrelated_host_socket(isolation):
    """An inaccessible inherited fd can still change host behavior.

    Once the parent's writer closes, its reader should observe EOF while the
    sandbox remains alive. A duplicate writer in the worker suppresses EOF
    even when sandboxed code has no module capable of discovering that fd.
    """
    reader, writer = socket.socketpair()
    try:
        reader.settimeout(1.0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with ProcessSandbox(
                Policy(timeout=10.0),
                isolation=isolation,
                allow_degraded=True,
                close_fds=True,
            ):
                writer.close()
                assert reader.recv(1) == b""
    finally:
        reader.close()
        writer.close()


# The behaviour under test IS fork inheritance -- a spawned worker has no
# inherited descriptors to preserve.
@pytest.mark.fork_only
def test_worker_preserves_inherited_descriptors_by_default():
    """Legacy policy registrations may intentionally depend on fork state."""
    host, peer = socket.socketpair()
    try:
        fd = host.fileno()
        host_signature = _fd_signature(fd)
        policy = Policy(timeout=10.0)
        policy.fn(_fd_signature, name="fd_signature")

        with ProcessSandbox(policy, isolation="none") as ps:
            result = ps.exec(
                "worker_signature = fd_signature(fd)",
                namespace={"fd": fd},
            )

        assert result.error is None
        assert result.namespace["worker_signature"] == host_signature
    finally:
        host.close()
        peer.close()


def test_close_fds_bypasses_an_active_virtual_filesystem():
    """Host-side descriptor enumeration must use the real filesystem."""
    token = current_fs.set(VirtualFS({}))
    try:
        with ProcessSandbox(
            Policy(timeout=10.0),
            isolation="none",
            close_fds=True,
        ) as ps:
            result = ps.exec("answer = 6 * 7")
        assert result.error is None
        assert result.namespace["answer"] == 42
    finally:
        current_fs.reset(token)


def test_idle_worker_exits_when_parent_process_disappears():
    """The worker must observe EOF when an abrupt parent closes its pipe."""
    ctx = multiprocessing.get_context("fork")
    receive, report = ctx.Pipe(duplex=False)
    host = ctx.Process(target=_start_worker_then_abandon_parent, args=(report,))
    host.start()
    report.close()

    assert receive.poll(5.0), "host did not report the worker pid"
    worker_pid = receive.recv()
    receive.close()
    host.join(timeout=5.0)
    assert not host.is_alive()
    host.close()

    deadline = time.monotonic() + 5.0
    while _pid_exists(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.05)

    try:
        assert not _pid_exists(worker_pid)
    finally:
        if _pid_exists(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)


# The worker signals the host with os.kill(os.getppid(), ...), and under
# forkserver a worker's parent is the BROKER, not the embedding process --
# so the notification never arrives. A genuine semantic difference (see
# docs/forkserver-design.md), not the inherited-broker-state bug: the
# scenario it guards -- one worker's INHERITED endpoint suppressing
# another's EOF -- cannot arise where nothing is inherited.
@pytest.mark.no_forkserver
def test_idle_worker_exits_while_later_worker_remains_busy():
    """Another worker's inherited endpoint must not suppress control EOF."""
    ctx = multiprocessing.get_context("fork")
    receive, report = ctx.Pipe(duplex=False)
    host = ctx.Process(target=_start_two_workers_then_abandon_parent, args=(report,))
    host.start()
    report.close()

    assert receive.poll(10.0), "host did not report the worker pids"
    first_pid, second_pid, second_started = receive.recv()
    receive.close()
    host.join(timeout=5.0)
    assert not host.is_alive()
    host.close()
    assert second_started, "later worker did not begin its blocking execution"

    deadline = time.monotonic() + 5.0
    while _pid_exists(first_pid) and time.monotonic() < deadline:
        time.sleep(0.05)

    try:
        assert not _pid_exists(first_pid)
        assert _pid_exists(second_pid), "later worker must still be busy"
    finally:
        for pid in (first_pid, second_pid):
            if _pid_exists(pid):
                os.kill(pid, signal.SIGKILL)


# ------------------------------------------------------------------
# No filesystem
# ------------------------------------------------------------------


def test_no_filesystem():
    """ProcessSandbox with filesystem=None works (no file I/O)."""
    with ProcessSandbox(Policy(timeout=10.0)) as ps:
        result = ps.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


# ------------------------------------------------------------------
# Async
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aexec(root):
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        result = await ps.aexec("x = 3 * 7")
        assert result.error is None
        assert result.namespace["x"] == 21


# ------------------------------------------------------------------
# Tick counter
# ------------------------------------------------------------------


def test_ticks_reported(root):
    fs = IsolatedFS(root)
    with ProcessSandbox(Policy(timeout=10.0, tick_limit=10000), filesystem=fs) as ps:
        result = ps.exec("for i in range(10): pass")
        assert result.error is None
        assert result.ticks > 0


# ------------------------------------------------------------------
# Worker crash recovery
# ------------------------------------------------------------------


def test_worker_killed_during_exec(root):
    """If the worker is killed mid-execution, exec() returns an error."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        # Start a long-running exec in a thread
        results = [None]

        def run():
            results[0] = ps.exec("while True: pass")

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)

        # Kill the worker process
        os.kill(ps._process.pid, signal.SIGKILL)

        t.join(timeout=10.0)
        assert results[0] is not None
        assert results[0].error is not None
        assert (
            "died" in str(results[0].error).lower()
            or "timeout" in str(results[0].error).lower()
        )


def test_exec_after_worker_crash_respawns(root):
    """A crash costs the crashing execution, not the sandbox: the next
    exec() spawns a fresh worker transparently."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        r1 = ps.exec("x = 1")
        assert r1.error is None

        # Kill the worker
        os.kill(ps._process.pid, signal.SIGKILL)
        ps._process.join(timeout=5.0)

        # Next exec respawns and succeeds
        r2 = ps.exec("y = 2")
        assert r2.error is None
        assert r2.namespace["y"] == 2

    # After a clean shutdown, exec raises until re-entered
    with pytest.raises(RuntimeError, match="Worker process is not running"):
        ps.exec("z = 3")


def test_exec_after_shutdown_raises(root):
    """After explicit shutdown(), exec() raises instead of silently re-forking."""
    ps = ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root))
    with ps:
        r1 = ps.exec("x = 1")
        assert r1.error is None

        ps.shutdown()
        assert ps._process is None

        # exec should raise, not respawn
        with pytest.raises(RuntimeError, match="Worker process is not running"):
            ps.exec("y = 2")

    # Final cleanup
    assert ps._process is None


# ------------------------------------------------------------------
# Worker init failure
# ------------------------------------------------------------------


# Patches the parent and relies on fork to carry it into the child; a
# spawned worker re-imports pristine modules and never sees it.
@pytest.mark.fork_only
def test_worker_init_failure_reported(root):
    """If the worker fails to initialise, a RuntimeError is raised."""
    with patch(
        "sandtrap.process.platform.apply_isolation",
        side_effect=RuntimeError("test init failure"),
    ):
        with pytest.raises(RuntimeError, match="Worker failed to initialise"):
            ProcessSandbox(
                Policy(timeout=5.0), filesystem=IsolatedFS(root), isolation="auto"
            ).__enter__()


# ------------------------------------------------------------------
# Cancel edge cases
# ------------------------------------------------------------------


def test_cancel_before_exec(root):
    """cancel() before any exec() is a no-op."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        ps.cancel()  # Should not raise
        result = ps.exec("x = 1")
        assert result.error is None
        assert result.namespace["x"] == 1


def test_cancel_after_completion(root):
    """cancel() after exec() returns is a no-op."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        result = ps.exec("x = 1")
        assert result.error is None
        ps.cancel()  # Should not raise
        result2 = ps.exec("y = 2")
        assert result2.error is None


def test_double_cancel(root):
    """Calling cancel() twice is safe."""
    # Short timeout so the test finishes quickly even if cancel() doesn't
    # work (e.g. SIGUSR1 not handled in forked children on macOS 3.13+).
    with ProcessSandbox(Policy(timeout=5.0), filesystem=IsolatedFS(root)) as ps:
        results = [None]

        def run():
            results[0] = ps.exec("while True: pass")

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        ps.cancel()
        ps.cancel()  # Should not raise
        t.join(timeout=15.0)
        assert results[0] is not None
        assert results[0].error is not None


def test_cancel_no_process():
    """cancel() with no worker is a no-op."""
    ps = ProcessSandbox(Policy(timeout=10.0))
    ps.cancel()  # No worker spawned, should not raise


# ------------------------------------------------------------------
# Shutdown edge cases
# ------------------------------------------------------------------


def test_shutdown_after_worker_killed(root):
    """shutdown() after the worker is already dead is safe."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        ps.exec("x = 1")
        # Kill the worker
        os.kill(ps._process.pid, signal.SIGKILL)
        ps._process.join(timeout=5.0)
    # __exit__ calls shutdown() — should not raise


def test_double_shutdown(root):
    """Calling shutdown() twice is safe."""
    ps = ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root))
    with ps:
        ps.exec("x = 1")
        ps.shutdown()
        ps.shutdown()  # Should not raise


# ------------------------------------------------------------------
# Non-picklable result namespace
# ------------------------------------------------------------------


def test_non_picklable_result_namespace(psandbox):
    """Non-picklable values produced by sandboxed code are dropped."""
    # Lambda functions are not picklable
    result = psandbox.exec("fn = lambda: 42\nx = 99")
    assert result.error is None
    assert result.namespace["x"] == 99
    # lambda is not picklable — it should be dropped
    assert "fn" not in result.namespace


# ------------------------------------------------------------------
# mode="raw"
# ------------------------------------------------------------------


def test_mode_raw(root):
    """mode='raw' works with ProcessSandbox."""
    fs = IsolatedFS(root)
    with ProcessSandbox(Policy(timeout=10.0), filesystem=fs, mode="raw") as ps:
        result = ps.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


# ------------------------------------------------------------------
# St* reactivation across process boundary
# ------------------------------------------------------------------


def test_wrapped_mode_warns(root):
    with pytest.warns(DeprecationWarning, match="deprecated"):
        ProcessSandbox(
            Policy(timeout=10.0), filesystem=IsolatedFS(root), mode="wrapped"
        )


def test_raw_mode_does_not_warn(root, recwarn):
    ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root))
    assert [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)] == []


def test_stfunction_reactivated(wrapped_psandbox):
    """Sandbox-defined functions are reactivated after crossing the process boundary."""
    result = wrapped_psandbox.exec("def double(n):\n    return n * 2")
    assert result.error is None
    fn = result.namespace["double"]
    assert callable(fn)
    assert fn(21) == 42


def test_stclass_reactivated(wrapped_psandbox):
    """Sandbox-defined classes are reactivated and constructable."""
    result = wrapped_psandbox.exec(
        "class Doubler:\n    def run(self, n):\n        return n * 2"
    )
    assert result.error is None
    cls = result.namespace["Doubler"]
    obj = cls()
    assert obj.run(21) == 42


def test_stfunction_with_closure(wrapped_psandbox):
    """Functions with closure variables are reactivated correctly."""
    result = wrapped_psandbox.exec("factor = 3\ndef scale(n):\n    return n * factor")
    assert result.error is None
    fn = result.namespace["scale"]
    assert fn(10) == 30


# ------------------------------------------------------------------
# filter_namespace (shared utility)
# ------------------------------------------------------------------


def test_filter_namespace_none():
    """filter_namespace(None) returns None."""
    assert filter_namespace(None) is None


def test_filter_namespace_all_picklable():
    """All picklable values pass through."""
    ns = {"x": 1, "y": "hello", "z": [1, 2, 3]}
    result = filter_namespace(ns)
    assert result == ns


def test_filter_namespace_drops_unpicklable():
    """Non-picklable values are silently dropped."""
    ns = {"x": 1, "fn": lambda: None, "y": "hello"}
    result = filter_namespace(ns)
    assert result == {"x": 1, "y": "hello"}


def test_filter_namespace_empty_dict():
    """Empty dict returns empty dict."""
    assert filter_namespace({}) == {}


# ------------------------------------------------------------------
# VirtualFS
# ------------------------------------------------------------------


def test_virtualfs_with_process_sandbox():
    """ProcessSandbox works with VirtualFS instead of IsolatedFS."""
    fs = VirtualFS({})
    fs.write("/data.txt", b"hello from vfs")

    with ProcessSandbox(Policy(timeout=10.0), filesystem=fs) as ps:
        result = ps.exec("content = open('/data.txt').read()")
        assert result.error is None
        assert result.namespace["content"] == "hello from vfs"


# ------------------------------------------------------------------
# Non-picklable namespace warning on send
# ------------------------------------------------------------------


def test_non_picklable_namespace_warns(psandbox):
    """Sending non-picklable values emits RuntimeWarning."""
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        result = psandbox.exec("y = 42", namespace={"fn": lambda: None, "x": 1})

    assert result.error is None
    assert result.namespace["y"] == 42
    # Should have warned about 'fn'
    runtime_warnings = [x for x in w if issubclass(x.category, RuntimeWarning)]
    assert any("fn" in str(warning.message) for warning in runtime_warnings)


# ------------------------------------------------------------------
# Integration: policy flags → kernel isolation
# ------------------------------------------------------------------
#
# Asserted from what the worker REPORTS, not by intercepting what the parent
# passed. Intercepting meant patching apply_isolation here and relying on fork
# to carry the patch into the child, which pinned these to one start method --
# and left the behaviour unverified for workers created any other way, where
# the child re-imports a pristine module and never sees the patch.
#
# IsolationStatus carries allow_network / allow_host_fs / root for exactly
# this reason: an embedder verifying isolation wants to know what the worker
# was built with, not merely which mechanisms engaged.


def _needs_network():  # module-level: crosses to any worker, unlike a lambda
    return None


def _needs_host_fs():
    return None


def _status_for(policy, filesystem, **kwargs):
    with ProcessSandbox(
        policy, filesystem=filesystem, isolation="auto", allow_degraded=True, **kwargs
    ) as sb:
        return sb.exec("x = 1").isolation


def test_network_access_policy_reaches_isolation(root):
    policy = Policy(timeout=10.0)
    policy.fn(_needs_network, name="fetch", network_access=True)
    assert _status_for(policy, IsolatedFS(root)).allow_network is True


def test_host_fs_access_policy_reaches_isolation(root):
    policy = Policy(timeout=10.0)
    policy.fn(_needs_host_fs, name="save", host_fs_access=True)
    assert _status_for(policy, IsolatedFS(root)).allow_host_fs is True


def test_default_policy_grants_neither(root):
    status = _status_for(Policy(timeout=10.0), IsolatedFS(root))
    assert status.allow_network is False
    assert status.allow_host_fs is False


def test_both_flags_reach_isolation(root):
    policy = Policy(timeout=10.0)
    policy.fn(_needs_network, name="fetch", network_access=True)
    policy.fn(_needs_host_fs, name="save", host_fs_access=True)
    status = _status_for(policy, IsolatedFS(root))
    assert status.allow_network is True
    assert status.allow_host_fs is True


def test_virtual_fs_confines_no_real_path(root):
    """A purely virtual filesystem has no path for the kernel to restrict."""
    assert _status_for(Policy(timeout=10.0), VirtualFS({})).root is None


def test_isolatedfs_root_is_what_gets_confined(root):
    assert _status_for(Policy(timeout=10.0), IsolatedFS(root)).root == root


def test_a_read_only_filesystem_refuses_a_worker_write_at_open():
    """The refusal must arrive at open(), not at a close the garbage
    collector performs, where it would be swallowed and the write lost
    without a trace."""
    from monkeyfs import ReadOnlyFS, VirtualFS

    from sandtrap import Policy, sandbox

    fs = ReadOnlyFS(VirtualFS({}))
    with sandbox(Policy(timeout=10.0), isolation="process", filesystem=fs) as sb:
        result = sb.exec("open('/scribble.txt', 'w').write('nope')\n")
    assert result.error is not None
    assert isinstance(result.error, PermissionError)
    assert not fs.exists("/scribble.txt")


def test_write_permission_is_asked_of_the_path_not_the_root():
    """A composed filesystem: writable root, read-only mount under a
    prefix. The refusal must land on the mount and only there, at
    open()."""
    from monkeyfs import MountFS, ReadOnlyFS, VirtualFS

    from sandtrap import Policy, sandbox

    root = VirtualFS({})
    root.write("/work/keep.txt", b"x")
    fs = MountFS(root, {"/data": ReadOnlyFS(VirtualFS({}))})
    with sandbox(Policy(timeout=10.0), isolation="process", filesystem=fs) as sb:
        refused = sb.exec("open('/data/out.txt', 'w').write('nope')\n")
        allowed = sb.exec("f = open('/work/out.txt', 'w')\nf.write('yes')\nf.close()\n")
    assert refused.error is not None and "out.txt" in str(refused.error)
    assert not fs.exists("/data/out.txt")
    assert allowed.error is None
    assert fs.read("/work/out.txt") == b"yes"
