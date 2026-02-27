"""Tests for resource limits (memory + stdout caps)."""

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
