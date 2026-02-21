"""Tests for checkpoint injection (Phase 4)."""

import threading

from sandtrap import Policy, Sandbox
from sandtrap.errors import SbCancelled, SbTickLimit, SbTimeout


def test_while_true_times_out():
    policy = Policy()
    policy.timeout = 0.1
    sandbox = Sandbox(policy)
    result = sandbox.exec("while True: pass")
    assert isinstance(result.error, SbTimeout)


def test_nested_loops_time_out():
    policy = Policy()
    policy.timeout = 0.1
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
while True:
    for i in range(1000):
        pass
""")
    assert isinstance(result.error, SbTimeout)


def test_fast_code_no_timeout():
    policy = Policy()
    policy.timeout = 10.0
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
total = 0
for i in range(100):
    total += i
""")
    assert result.error is None
    assert result.namespace["total"] == 4950


def test_cancellation_via_flag():
    policy = Policy()
    policy.timeout = 10.0
    sandbox = Sandbox(policy)

    cancel = threading.Event()
    cancel.set()  # Pre-set: should cancel immediately

    # Inject cancel flag via internal API
    import time

    from sandtrap.gates import make_gates

    gates = make_gates(policy, _start_time=time.monotonic(), _cancel_flag=cancel)

    sandbox.exec("x = 1")  # Normal exec won't use our cancel flag

    # Test with direct gate call
    try:
        gates["__st_checkpoint__"]()
        assert False, "Should have raised SbCancelled"
    except SbCancelled:
        pass


def test_function_has_checkpoint():
    """Functions get checkpoint at entry — recursive function times out."""
    policy = Policy()
    policy.timeout = 0.1
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
def recurse(n):
    return recurse(n + 1)
recurse(0)
""")
    # Should timeout (via checkpoint) or hit recursion limit
    assert result.error is not None


def test_for_loop_checkpoint():
    """For loops get checkpoint at start of body."""
    policy = Policy()
    policy.timeout = 0.1
    sandbox = Sandbox(policy)
    # Use a custom iterator that never ends
    result = sandbox.exec("""\
def infinite():
    i = 0
    while True:
        yield i
        i += 1

for x in infinite():
    pass
""")
    assert isinstance(result.error, SbTimeout)


def test_no_timeout_when_none():
    """When timeout is None, no timeout checking occurs."""
    policy = Policy()
    policy.timeout = None
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
total = 0
for i in range(1000):
    total += i
""")
    assert result.error is None
    assert result.namespace["total"] == 499500


def test_cancel_flag_via_sandbox():
    """sandbox.cancel_flag can cancel a running execution from another thread."""
    policy = Policy()
    policy.timeout = 10.0
    sandbox = Sandbox(policy)

    # Cancel from a timer thread after a short delay
    timer = threading.Timer(0.05, sandbox.cancel)
    timer.start()

    result = sandbox.exec("""\
for i in range(10_000_000):
    pass
""")
    timer.cancel()
    assert isinstance(result.error, SbCancelled)


def test_tick_limit_triggers():
    """Loop exceeding tick limit raises SbTickLimit."""
    policy = Policy(tick_limit=50)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
for i in range(200):
    pass
""")
    assert isinstance(result.error, SbTickLimit)


def test_tick_limit_allows_fast_code():
    """Short loop under tick limit succeeds."""
    policy = Policy(tick_limit=1000)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
total = 0
for i in range(10):
    total += i
""")
    assert result.error is None
    assert result.namespace["total"] == 45


def test_tick_count_on_result():
    """result.ticks reflects the actual checkpoint count."""
    policy = Policy(timeout=None)
    sandbox = Sandbox(policy)
    # range(5) = 1 tick, + 5 loop iterations = 6 ticks
    result = sandbox.exec("""\
for i in range(5):
    pass
""")
    assert result.error is None
    assert result.ticks == 6


def test_tick_limit_none_no_enforcement():
    """tick_limit=None means no tick enforcement."""
    policy = Policy(tick_limit=None)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
for i in range(10000):
    pass
""")
    assert result.error is None
    assert result.ticks == 10001


def test_comprehension_respects_tick_limit():
    """Comprehension ticks count against tick_limit."""
    policy = Policy(tick_limit=50)
    sandbox = Sandbox(policy)
    result = sandbox.exec("[i for i in range(200)]")
    assert isinstance(result.error, SbTickLimit)
