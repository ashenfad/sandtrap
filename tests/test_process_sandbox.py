"""Tests for ProcessSandbox — subprocess-backed execution."""

import os
import signal
import threading
import time
from unittest.mock import patch

import pytest
from monkeyfs import IsolatedFS, VirtualFS

from sandtrap import Policy
from sandtrap.process.protocol import filter_namespace
from sandtrap.process.sandbox import ProcessSandbox


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


@pytest.fixture
def psandbox(root):
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
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
    with ProcessSandbox(Policy(timeout=30.0), filesystem=IsolatedFS(root)) as ps:
        results = [None]

        def run():
            results[0] = ps.exec("while True: pass")

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        ps.cancel()
        t.join(timeout=10.0)
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
    """After a worker crash, the next exec() spawns a new worker."""
    with ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root)) as ps:
        r1 = ps.exec("x = 1")
        assert r1.error is None

        # Kill the worker
        os.kill(ps._process.pid, signal.SIGKILL)
        ps._process.join(timeout=5.0)

        # Next exec should auto-respawn
        r2 = ps.exec("y = 2")
        assert r2.error is None
        assert r2.namespace["y"] == 2


def test_exec_after_shutdown_respawns(root):
    """After explicit shutdown(), the next exec() spawns a new worker."""
    ps = ProcessSandbox(Policy(timeout=10.0), filesystem=IsolatedFS(root))
    with ps:
        r1 = ps.exec("x = 1")
        assert r1.error is None

        ps.shutdown()
        assert ps._process is None

        # exec should respawn
        r2 = ps.exec("y = 2")
        assert r2.error is None
        assert r2.namespace["y"] == 2

    # Final cleanup
    assert ps._process is None


# ------------------------------------------------------------------
# Worker init failure
# ------------------------------------------------------------------


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
    with ProcessSandbox(Policy(timeout=30.0), filesystem=IsolatedFS(root)) as ps:
        results = [None]

        def run():
            results[0] = ps.exec("while True: pass")

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        ps.cancel()
        ps.cancel()  # Should not raise
        t.join(timeout=10.0)
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
# Integration: policy flags → apply_isolation
# ------------------------------------------------------------------


def _read_isolation_args(marker_path):
    """Read the isolation args recorded by the child process."""
    import json

    with open(marker_path) as f:
        return json.load(f)


def _recording_apply_isolation(marker_path):
    """Return a replacement for apply_isolation that records args to a file."""
    import json

    def _record(mode, root, *, allow_network=False, allow_host_fs=False):
        with open(marker_path, "w") as f:
            json.dump(
                {
                    "mode": mode,
                    "root": root,
                    "allow_network": allow_network,
                    "allow_host_fs": allow_host_fs,
                },
                f,
            )

    return _record


def test_network_access_policy_forwards_to_isolation(root):
    """A policy with network_access=True passes allow_network=True to apply_isolation."""
    policy = Policy(timeout=10.0)
    policy.fn(lambda: None, name="fetch", network_access=True)

    marker = os.path.join(root, "_isolation_args.json")
    with patch(
        "sandtrap.process.platform.apply_isolation",
        new=_recording_apply_isolation(marker),
    ):
        with ProcessSandbox(policy, filesystem=IsolatedFS(root), isolation="auto"):
            pass

    args = _read_isolation_args(marker)
    assert args["allow_network"] is True


def test_host_fs_access_policy_forwards_to_isolation(root):
    """A policy with host_fs_access=True passes allow_host_fs=True to apply_isolation."""
    policy = Policy(timeout=10.0)
    policy.fn(lambda: None, name="save", host_fs_access=True)

    marker = os.path.join(root, "_isolation_args.json")
    with patch(
        "sandtrap.process.platform.apply_isolation",
        new=_recording_apply_isolation(marker),
    ):
        with ProcessSandbox(policy, filesystem=IsolatedFS(root), isolation="auto"):
            pass

    args = _read_isolation_args(marker)
    assert args["allow_host_fs"] is True


def test_default_policy_no_network_no_host_fs(root):
    """Default policy passes allow_network=False, allow_host_fs=False."""
    policy = Policy(timeout=10.0)

    marker = os.path.join(root, "_isolation_args.json")
    with patch(
        "sandtrap.process.platform.apply_isolation",
        new=_recording_apply_isolation(marker),
    ):
        with ProcessSandbox(policy, filesystem=IsolatedFS(root), isolation="auto"):
            pass

    args = _read_isolation_args(marker)
    assert args["allow_network"] is False
    assert args["allow_host_fs"] is False


def test_both_flags_forwarded(root):
    """Policy with both network and host_fs access forwards both flags."""
    policy = Policy(timeout=10.0)
    policy.fn(lambda: None, name="fetch", network_access=True)
    policy.fn(lambda: None, name="save", host_fs_access=True)

    marker = os.path.join(root, "_isolation_args.json")
    with patch(
        "sandtrap.process.platform.apply_isolation",
        new=_recording_apply_isolation(marker),
    ):
        with ProcessSandbox(policy, filesystem=IsolatedFS(root), isolation="auto"):
            pass

    args = _read_isolation_args(marker)
    assert args["allow_network"] is True
    assert args["allow_host_fs"] is True


def test_filesystem_param_passes_root_none_to_isolation(root):
    """When using VirtualFS, root=None is passed to apply_isolation."""
    policy = Policy(timeout=10.0)
    fs = VirtualFS({})

    marker = os.path.join(root, "_isolation_args.json")
    with patch(
        "sandtrap.process.platform.apply_isolation",
        new=_recording_apply_isolation(marker),
    ):
        with ProcessSandbox(policy, filesystem=fs, isolation="auto"):
            pass

    args = _read_isolation_args(marker)
    assert args["root"] is None


def test_isolatedfs_root_extracted_for_isolation(root):
    """When using IsolatedFS, root path is extracted and passed to apply_isolation."""
    policy = Policy(timeout=10.0)

    marker = os.path.join(root, "_isolation_args.json")
    with patch(
        "sandtrap.process.platform.apply_isolation",
        new=_recording_apply_isolation(marker),
    ):
        with ProcessSandbox(policy, filesystem=IsolatedFS(root), isolation="auto"):
            pass

    args = _read_isolation_args(marker)
    assert args["root"] is not None
    assert args["root"] == root
