"""Tests for ``timeout`` on ``exec`` and ``aexec``: one execution's
wall-clock budget, in place of the policy's."""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from sandtrap import IsolationUnavailable, Policy, StTimeout, sandbox

ISOLATIONS = ("none", "process", "kernel")
RUNNERS = ("exec", "aexec")


def _sandbox(isolation, timeout):
    policy = Policy(timeout=timeout)
    policy.module(time)
    try:
        return sandbox(policy, isolation=isolation)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


def _run(sb, runner, source, **kwargs):
    if runner == "aexec":
        return asyncio.run(sb.aexec(source, **kwargs))
    return sb.exec(source, **kwargs)


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_shorter_timeout_stops_the_execution(isolation, runner):
    with _sandbox(isolation, 30.0) as sb:
        result = _run(sb, runner, "while True: pass", timeout=0.3)
    assert isinstance(result.error, StTimeout)
    assert "0.3s timeout" in str(result.error)


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_longer_timeout_outlasts_the_policys(isolation, runner):
    with _sandbox(isolation, 0.5) as sb:
        result = _run(
            sb,
            runner,
            "import time\nt0 = time.monotonic()\n"
            "while time.monotonic() - t0 < 1.0: pass\nok = True",
            timeout=10.0,
        )
        assert result.error is None, result.error
        assert result.namespace["ok"] is True
        # the next execution has the policy's budget again
        result = _run(sb, runner, "while True: pass")
    assert "0.5s timeout" in str(result.error)


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
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_timeout_that_is_not_a_positive_finite_number_is_refused(
    isolation, bad, error
):
    with _sandbox(isolation, 30.0) as sb:
        with pytest.raises(error, match="timeout"):
            sb.exec("1", timeout=bad)
