"""Tests for resource limits (memory + stdout caps)."""

import multiprocessing
import sys

import pytest

from sandtrap import Policy, Sandbox
from sandtrap.builtins import TailBuffer

# --- TailBuffer unit tests ---


def test_tail_buffer_no_limit():
    """Without a limit, TailBuffer behaves like StringIO."""
    buf = TailBuffer()
    buf.write("hello ")
    buf.write("world")
    assert buf.getvalue() == "hello world"


def test_tail_buffer_within_limit():
    """Output within the limit is returned verbatim."""
    buf = TailBuffer(max_chars=100)
    buf.write("short")
    assert buf.getvalue() == "short"


def test_tail_buffer_keeps_tail():
    """When output exceeds limit, only the tail is kept."""
    buf = TailBuffer(max_chars=10)
    buf.write("a" * 20)
    value = buf.getvalue()
    # Should have truncation marker + last 10 chars
    assert "truncated" in value
    assert value.endswith("a" * 10)


def test_tail_buffer_incremental_writes():
    """Multiple writes that eventually exceed the limit."""
    buf = TailBuffer(max_chars=10)
    for i in range(20):
        buf.write(f"{i}\n")
    value = buf.getvalue()
    assert "truncated" in value
    # The last few numbers should be present
    assert "19\n" in value


def test_tail_buffer_no_marker_when_not_truncated():
    """No truncation marker when output fits within limit."""
    buf = TailBuffer(max_chars=100)
    buf.write("fits fine")
    assert "truncated" not in buf.getvalue()


# --- Stdout limit in sandbox ---


def test_sandbox_stdout_limit():
    """Sandbox respects max_stdout policy."""
    policy = Policy()
    policy.max_stdout = 50
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
for i in range(1000):
    print(i)
""")
    assert result.error is None
    assert "truncated" in result.stdout
    # The tail should contain the last numbers
    assert "999" in result.stdout


def test_sandbox_no_stdout_limit():
    """Without max_stdout, all output is captured."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
for i in range(100):
    print(i)
""")
    assert result.error is None
    assert "0\n" in result.stdout
    assert "99\n" in result.stdout
    assert "truncated" not in result.stdout


# --- Memory limit tests ---


def test_memory_limit_allows_reasonable_allocation():
    """Memory limit allows normal-sized allocations."""
    policy = Policy()
    policy.memory_limit = 200  # 200 MB headroom
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
x = [0] * 100_000
result = len(x)
""")
    assert result.error is None
    assert result.namespace["result"] == 100_000


def test_no_memory_limit():
    """Without memory_limit set, no MemoryError is raised."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
x = [0] * 1_000_000
result = len(x)
""")
    assert result.error is None
    assert result.namespace["result"] == 1_000_000


# --- Memory limit enforcement ---


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is Linux-only")
def test_rlimit_as_blocks_single_large_allocation():
    """RLIMIT_AS catches a single allocation that exceeds the limit."""
    policy = Policy()
    policy.memory_limit = 10  # 10 MB headroom
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = bytearray(100 * 1024 * 1024)")  # 100 MB
    assert isinstance(result.error, MemoryError)


def _run_checkpoint_memory_test(result_conn):
    """Run in a subprocess so peak RSS starts from a clean baseline."""
    policy = Policy()
    policy.memory_limit = 10  # 10 MB headroom
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
chunks = []
for i in range(20):
    chunks.append(bytearray(4 * 1024 * 1024))  # 4 MB each
""")
    result_conn.send(type(result.error).__name__ if result.error else "none")


@pytest.mark.skipif(sys.platform == "win32", reason="No resource module on Windows")
def test_checkpoint_memory_enforcement():
    """Checkpoint-based memory detection catches gradual growth.

    Runs in a subprocess so the peak RSS baseline is clean and not
    inflated by prior test allocations.
    """
    parent_conn, child_conn = multiprocessing.Pipe()
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=_run_checkpoint_memory_test, args=(child_conn,))
    p.start()
    p.join(timeout=30)
    assert p.exitcode == 0, f"Subprocess exited with code {p.exitcode}"
    error_name = parent_conn.recv()
    assert error_name == "MemoryError"
