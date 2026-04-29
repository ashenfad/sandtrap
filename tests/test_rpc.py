"""Tests for the worker↔parent RPC protocol used by host-side handlers
to back ``RpcProxyMarker`` namespace entries.

The full picture: under process / kernel isolation the parent injects
``RpcProxyMarker(target="...")`` placeholders into the namespace.
The worker substitutes each marker with an ``RpcProxy`` that, on each
method call, sends an ``RpcCallMsg`` to the parent and blocks on the
matching ``RpcReturnMsg``.  The parent's ``ProcessSandbox.exec``
dispatch loop runs the registered handler and replies.
"""

from __future__ import annotations

import multiprocessing

import pytest

from sandtrap import Policy, RpcProxyMarker, sandbox

# Spawn a fresh fork context; the existing sandbox tests already use
# fork — match them.
_CTX = multiprocessing.get_context("fork")


# ---------------------------------------------------------------------------
# Bare RpcProxy: agent calls a method, handler returns the value
# ---------------------------------------------------------------------------


def test_basic_rpc_round_trip():
    """A raw RpcProxy method call reaches the handler and returns."""
    seen: list[tuple[str, tuple, dict]] = []

    def cache_handler(method, args, kwargs):
        seen.append((method, args, kwargs))
        if method == "get":
            return {"k": "v"}.get(args[0])
        raise AttributeError(method)

    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={"cache": cache_handler},
    ) as sb:
        result = sb.exec(
            "got = cache.get('k')",
            namespace={"cache": RpcProxyMarker(target="cache")},
        )
        assert result.error is None
        assert result.namespace["got"] == "v"

    assert seen == [("get", ("k",), {})]


# Module-level so pickle can resolve the class on the worker side
# (locally-defined classes inside a test function don't pickle).
class _RpcCustomError(RuntimeError):
    """Custom exception used by ``test_handler_exceptions_propagate``."""


def test_handler_exceptions_propagate():
    """A handler-raised exception surfaces in the agent's code."""

    def cache_handler(method, args, kwargs):
        raise _RpcCustomError("nope")

    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={"cache": cache_handler},
    ) as sb:
        result = sb.exec(
            "x = cache.get('anything')",
            namespace={"cache": RpcProxyMarker(target="cache")},
        )
        # Exception escapes the agent code — sandtrap captures it
        # in result.error like any other agent-raised exception.
        assert isinstance(result.error, _RpcCustomError)
        assert "nope" in str(result.error)


def test_unregistered_target_raises():
    """A marker whose target has no handler errors clearly."""
    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={},  # nothing registered
    ) as sb:
        result = sb.exec(
            "x = cache.get('k')",
            namespace={"cache": RpcProxyMarker(target="cache")},
        )
        assert isinstance(result.error, RuntimeError)
        assert "no rpc handler registered" in str(result.error)


def test_multiple_targets_dispatch_independently():
    """Two markers with different targets reach the right handlers."""
    state_a: dict = {}
    state_b: dict = {}

    def handler_a(method, args, kwargs):
        if method == "set":
            state_a[args[0]] = args[1]
        elif method == "get":
            return state_a[args[0]]
        else:
            raise AttributeError(method)

    def handler_b(method, args, kwargs):
        if method == "set":
            state_b[args[0]] = args[1]
        elif method == "get":
            return state_b[args[0]]
        else:
            raise AttributeError(method)

    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={"a": handler_a, "b": handler_b},
    ) as sb:
        result = sb.exec(
            "a.set('x', 1)\nb.set('x', 2)\ngot_a = a.get('x')\ngot_b = b.get('x')\n",
            namespace={
                "a": RpcProxyMarker(target="a"),
                "b": RpcProxyMarker(target="b"),
            },
        )
        assert result.error is None
        assert result.namespace["got_a"] == 1
        assert result.namespace["got_b"] == 2

    # Handlers wrote to their own state — no cross-contamination.
    assert state_a == {"x": 1}
    assert state_b == {"x": 2}


# ---------------------------------------------------------------------------
# Wrapped proxy: marker.wrapper points at a class the worker imports
# ---------------------------------------------------------------------------


# Module-level wrapper class so it's importable by dotted path from
# the worker process (lambdas / closures wouldn't work — pickle and
# importlib both need a real symbol).
class _Counter:
    """Minimal MutableMapping-ish wrapper around an RpcProxy.

    Used by the wrapper-substitution test to verify that
    ``marker.wrapper`` resolves correctly inside the worker.
    """

    def __init__(self, proxy):
        self._proxy = proxy

    def incr(self, key):
        return self._proxy._call("incr", key)

    def value(self, key):
        return self._proxy._call("value", key)


def test_wrapper_substitution():
    """``marker.wrapper`` instantiates a typed wrapper around the proxy."""
    counts: dict[str, int] = {}

    def handler(method, args, kwargs):
        if method == "incr":
            counts[args[0]] = counts.get(args[0], 0) + 1
            return counts[args[0]]
        if method == "value":
            return counts.get(args[0], 0)
        raise AttributeError(method)

    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={"counter": handler},
    ) as sb:
        result = sb.exec(
            "n1 = counter.incr('a')\nn2 = counter.incr('a')\nv = counter.value('a')\n",
            namespace={
                "counter": RpcProxyMarker(
                    target="counter",
                    wrapper="tests.test_rpc:_Counter",
                ),
            },
        )
        assert result.error is None
        assert result.namespace["n1"] == 1
        assert result.namespace["n2"] == 2
        assert result.namespace["v"] == 2


def test_wrapper_resolution_failure_falls_back_to_bare_proxy():
    """If the wrapper dotted path doesn't import, the worker falls
    back to the bare ``RpcProxy`` so the agent still gets *something*
    callable rather than a hard worker-crash."""

    def handler(method, args, kwargs):
        if method == "ping":
            return "pong"
        raise AttributeError(method)

    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={"x": handler},
    ) as sb:
        result = sb.exec(
            "got = x.ping()",
            namespace={
                "x": RpcProxyMarker(
                    target="x",
                    wrapper="not_a_module:Whatever",
                ),
            },
        )
        # Bare proxy still works — agent's ``x.ping()`` reached the
        # handler.
        assert result.error is None
        assert result.namespace["got"] == "pong"


# ---------------------------------------------------------------------------
# Many calls in a row — verify dispatch loop is stable under load
# ---------------------------------------------------------------------------


def test_many_sequential_rpc_calls():
    """100 round-trips don't deadlock or accumulate state in the loop."""

    def handler(method, args, kwargs):
        if method == "echo":
            return args[0]
        raise AttributeError(method)

    policy = Policy(timeout=10.0, tick_limit=10_000_000)
    with sandbox(
        policy,
        isolation="process",
        rpc_handlers={"e": handler},
    ) as sb:
        result = sb.exec(
            "vals = [e.echo(i) for i in range(100)]",
            namespace={"e": RpcProxyMarker(target="e")},
        )
        assert result.error is None
        assert result.namespace["vals"] == list(range(100))


# ---------------------------------------------------------------------------
# Forward-compat: unknown protocol message types are warned, not fatal
# ---------------------------------------------------------------------------


def test_no_handlers_dict_works():
    """Sandbox without rpc_handlers still works for non-RPC code."""
    policy = Policy(timeout=5.0)
    with sandbox(policy, isolation="process") as sb:
        result = sb.exec("x = 1 + 1")
        assert result.error is None
        assert result.namespace["x"] == 2


@pytest.mark.parametrize("isolation", ["process", "kernel"])
def test_rpc_works_under_kernel_isolation(isolation):
    """RPC traverses the connection cleanly even with kernel-level
    sandboxing engaged.  The Connection's file descriptor is set up
    before seccomp/Landlock/Seatbelt apply, so reads/writes through
    it should still work."""
    if isolation == "kernel":
        # Kernel mode is platform-specific; skip on platforms where
        # it's a no-op stub (Windows etc.) — but Darwin and Linux,
        # which our test matrix covers, do apply isolation.
        pass

    def handler(method, args, kwargs):
        if method == "ping":
            return "pong"
        raise AttributeError(method)

    policy = Policy(timeout=5.0)
    with sandbox(
        policy,
        isolation=isolation,
        rpc_handlers={"p": handler},
    ) as sb:
        result = sb.exec(
            "got = p.ping()",
            namespace={"p": RpcProxyMarker(target="p")},
        )
        assert result.error is None
        assert result.namespace["got"] == "pong"
