"""Tests for async sandbox execution (Phase 7)."""


import pytest

from sblite import Policy, Sandbox
from sblite.errors import SbTimeout


@pytest.fixture
def sandbox():
    return Sandbox(Policy())


@pytest.mark.asyncio
async def test_simple_async_exec(sandbox):
    result = await sandbox.aexec("x = 2 + 3")
    assert result.error is None
    assert result.namespace["x"] == 5


@pytest.mark.asyncio
async def test_await_expression():
    import asyncio as _asyncio

    policy = Policy()
    policy.module(_asyncio)
    sandbox = Sandbox(policy)
    result = await sandbox.aexec("""\
import asyncio
await asyncio.sleep(0)
x = 42
""")
    assert result.error is None
    assert result.namespace["x"] == 42


@pytest.mark.asyncio
async def test_async_def_and_call(sandbox):
    result = await sandbox.aexec("""\
async def double(x):
    return x * 2

result = await double(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 10


@pytest.mark.asyncio
async def test_async_print_capture(sandbox):
    result = await sandbox.aexec("print('async hello')")
    assert result.error is None
    assert result.stdout == "async hello\n"


@pytest.mark.asyncio
async def test_async_timeout():
    import asyncio as _asyncio

    policy = Policy()
    policy.timeout = 0.1
    policy.module(_asyncio)
    sandbox = Sandbox(policy)
    result = await sandbox.aexec("""\
import asyncio
await asyncio.sleep(10)
""")
    assert isinstance(result.error, SbTimeout)


@pytest.mark.asyncio
async def test_async_error_captured(sandbox):
    result = await sandbox.aexec("x = 1 / 0")
    assert isinstance(result.error, ZeroDivisionError)


@pytest.mark.asyncio
async def test_async_for(sandbox):
    result = await sandbox.aexec("""\
async def arange(n):
    for i in range(n):
        yield i

total = 0
async for x in arange(5):
    total += x
""")
    assert result.error is None
    assert result.namespace["total"] == 10


@pytest.mark.asyncio
async def test_async_with_sync_code(sandbox):
    """aexec works fine with purely synchronous code too."""
    result = await sandbox.aexec("""\
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)

result = factorial(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 120


@pytest.mark.asyncio
async def test_async_result_excludes_internals(sandbox):
    """aexec result namespace doesn't contain __sb_* keys."""
    result = await sandbox.aexec("x = 42")
    assert result.error is None
    assert result.namespace["x"] == 42
    for key in result.namespace:
        assert not key.startswith("__sb_"), f"Internal key leaked: {key}"


@pytest.mark.asyncio
async def test_async_cancellation():
    """Cancelling an async sandbox execution via sandbox.cancel()."""
    import threading

    from sblite.errors import SbCancelled

    policy = Policy()
    sandbox = Sandbox(policy)

    # Use a thread because the while loop blocks the event loop
    timer = threading.Timer(0.05, sandbox.cancel)
    timer.start()
    try:
        result = await sandbox.aexec("while True: pass")
    finally:
        timer.cancel()
    assert result.error is not None
    assert isinstance(result.error, SbCancelled)
