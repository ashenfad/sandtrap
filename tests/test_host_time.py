"""Host time: the timeout bounds the sandboxed code, not the host it calls."""

from __future__ import annotations

import asyncio
import multiprocessing
import time

import pytest

from sandtrap import MemberSpec, Policy, RpcProxyMarker, Sandbox, host_time, sandbox
from sandtrap.clock import HostClock
from sandtrap.errors import StTimeout

_CTX = multiprocessing.get_context("fork")

# Two calls of WAIT each fit the timeout; together they don't.
WAIT = 0.4
TIMEOUT = 0.6

TWO_CALLS = """\
a = h.wait()
b = h.wait()
for i in range(1000):
    pass
"""


class Slow:
    def wait(self, seconds: float = WAIT) -> str:
        time.sleep(seconds)
        return "done"

    async def await_(self, seconds: float = WAIT) -> str:
        await asyncio.sleep(seconds)
        return "done"


def _policy(**cls_kwargs) -> Policy:
    policy = Policy(timeout=TIMEOUT)
    policy.cls(Slow, name="Slow", **cls_kwargs)
    return policy


def test_a_host_time_class_is_off_the_clock():
    sb = Sandbox(_policy(host_time=True))
    result = sb.exec(TWO_CALLS, namespace={"h": Slow()})
    assert result.error is None
    assert result.namespace["b"] == "done"


def test_an_unmarked_class_is_the_codes_time():
    """A registration is library code unless the embedder says it's the
    host's: the sandbox can't tell numpy from a host object."""
    sb = Sandbox(_policy())
    result = sb.exec(TWO_CALLS, namespace={"h": Slow()})
    assert isinstance(result.error, StTimeout)


def test_a_member_can_be_marked_alone():
    sb = Sandbox(_policy(configure={"wait": MemberSpec(host_time=True)}))
    result = sb.exec(TWO_CALLS, namespace={"h": Slow()})
    assert result.error is None


def test_a_host_time_function_is_off_the_clock():
    policy = Policy(timeout=TIMEOUT)

    def wait() -> str:
        time.sleep(WAIT)
        return "done"

    policy.fn(wait, host_time=True)
    result = Sandbox(policy).exec("wait()\nwait()\nfor i in range(1000):\n    pass")
    assert result.error is None


def test_the_code_after_a_host_call_is_still_timed():
    sb = Sandbox(_policy(host_time=True))
    result = sb.exec("h.wait()\nwhile True:\n    pass", namespace={"h": Slow()})
    assert isinstance(result.error, StTimeout)


@pytest.mark.asyncio
async def test_an_awaited_host_call_is_off_the_clock():
    sb = Sandbox(_policy(host_time=True))
    result = await sb.aexec(
        "a = await h.await_()\nb = await h.await_()", namespace={"h": Slow()}
    )
    assert result.error is None
    assert result.namespace["b"] == "done"


@pytest.mark.asyncio
async def test_an_await_after_a_host_call_is_still_timed():
    policy = _policy(host_time=True)
    policy.module(asyncio)
    sb = Sandbox(policy)
    result = await sb.aexec(
        "import asyncio\nawait h.await_()\nawait asyncio.sleep(10)",
        namespace={"h": Slow()},
    )
    assert isinstance(result.error, StTimeout)


def test_an_rpc_is_host_time_under_process_isolation():
    """The parent already stretched its own deadline by the handler's
    time; the worker's checkpoint has to agree, or the call is charged
    anyway."""

    def handler(method, args, kwargs):
        time.sleep(WAIT)
        return "done"

    with sandbox(
        Policy(timeout=TIMEOUT),
        isolation="process",
        rpc_handlers={"h": handler},
    ) as sb:
        result = sb.exec(TWO_CALLS, namespace={"h": RpcProxyMarker(target="h")})
    assert result.error is None
    assert result.namespace["b"] == "done"


def test_overlapping_calls_refund_once():
    start = [100.0]
    clock = HostClock(start)
    clock.enter()
    clock.enter()
    time.sleep(0.05)
    clock.exit()
    assert clock.paused and start[0] == 100.0
    clock.exit()
    assert not clock.paused
    assert 100.04 < start[0] < 101.0


def test_host_time_outside_an_execution_is_a_no_op():
    with host_time():
        pass
