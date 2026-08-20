"""What an RPC proxy does with names that aren't method calls.

The bridge carries method calls. It never carried attribute reads or writes —
but it didn't *say* so, and the ways it failed were all silent or misleading:

    c.token       -> None      (a callable, dropped as unpicklable on the way back)
    c.limit + 1   -> TypeError: unsupported operand type(s) for 'function' and 'int'
    c.limit = 99  -> succeeds; the host object is untouched

A proxy can't inspect the object it stands for, so it can't tell a method from
a data attribute on its own. ``rpc_surface`` computes that split parent-side,
where the object lives, and the marker carries it — which also gives the
proxy the only place a policy's member filters reach a bridged object.
"""

import pytest

from sandtrap import Policy, RpcProxyMarker, rpc_surface
from sandtrap.process.sandbox import ProcessSandbox


class Client:
    token = "sekrit"
    limit = 10

    def query(self, q):
        return f"rows for {q}"

    def _hidden(self):
        return "no"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def policy():
    p = Policy(timeout=15.0)
    p.cls(Client, include=("query", "token", "limit"))
    return p


def _handler(obj):
    def handler(method, args, kwargs):
        attr = getattr(obj, method)
        return attr(*args, **kwargs) if callable(attr) else attr

    return handler


def run(policy, client, code, marker):
    with ProcessSandbox(
        policy, isolation="none", rpc_handlers={"host:c": _handler(client)}
    ) as sb:
        return sb.exec(code, namespace={"c": marker})


# -- the surface split --------------------------------------------------------


def test_rpc_surface_splits_methods_from_data(client):
    methods, attributes = rpc_surface(client)
    assert "query" in methods
    assert set(attributes) >= {"token", "limit"}
    assert not any(n.startswith("_") for n in methods + attributes)


def test_rpc_surface_narrows_to_the_policy(client):
    """The only place a policy's member filters reach a bridged object: the
    proxy refuses by name, because nothing else can."""
    p = Policy(timeout=5.0)
    p.cls(Client, include=("query",))
    methods, attributes = rpc_surface(client, p)
    assert methods == ("query",)
    assert attributes == ()


def test_rpc_surface_skips_attributes_that_raise():
    class Awkward:
        @property
        def boom(self):
            raise RuntimeError("side effect")

        def fine(self):
            return 1

    methods, attributes = rpc_surface(Awkward())
    assert methods == ("fine",)
    assert "boom" not in attributes  # skipped, not fatal


# -- what the proxy does with the surface -------------------------------------


def declared(client, policy) -> RpcProxyMarker:
    methods, attributes = rpc_surface(client, policy)
    return RpcProxyMarker(target="host:c", methods=methods, attributes=attributes)


def test_method_calls_still_work(policy, client):
    marker = declared(client, policy)
    assert (
        run(policy, client, "v = c.query('x')", marker).namespace["v"] == "rows for x"
    )


def test_reading_a_data_attribute_says_why(policy, client):
    marker = declared(client, policy)
    result = run(policy, client, "v = c.token", marker)
    assert isinstance(result.error, AttributeError)
    message = str(result.error)
    assert "data attribute" in message
    assert "method calls only" in message  # what the bridge does carry


def test_arithmetic_on_a_data_attribute_fails_at_the_read(policy, client):
    """Previously surfaced as 'unsupported operand for function and int',
    which points nowhere near the cause."""
    marker = declared(client, policy)
    result = run(policy, client, "v = c.limit + 1", marker)
    assert isinstance(result.error, AttributeError)
    assert "limit" in str(result.error)


def test_an_undeclared_name_lists_what_is_available(policy, client):
    marker = declared(client, policy)
    result = run(policy, client, "v = c.nope()", marker)
    assert isinstance(result.error, AttributeError)
    assert "query" in str(result.error)  # names the real surface


# -- writes, declared surface or not ------------------------------------------


@pytest.mark.parametrize("with_surface", [True, False])
def test_assignment_is_refused_rather_than_lost(policy, client, with_surface):
    """The worst of the three: the write landed on the proxy and the host
    object kept its old value, with nothing to indicate anything was lost.
    Refused whether or not a surface was declared."""
    marker = declared(client, policy) if with_surface else RpcProxyMarker("host:c")

    result = run(policy, client, "c.limit = 99", marker)
    assert isinstance(result.error, AttributeError)
    assert "would be lost" in str(result.error)
    assert client.limit == 10  # unchanged, and now visibly so


def test_undeclared_markers_keep_working(policy, client):
    """Back-compat: an embedder that hasn't declared a surface still gets
    method calls. Only the silent write is taken away."""
    marker = RpcProxyMarker(target="host:c")
    assert run(policy, client, "v = c.query('x')", marker).namespace["v"] == (
        "rows for x"
    )
