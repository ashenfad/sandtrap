"""Tests for network interception (Phase 8)."""

import socket

import pytest

from sandtrap import Policy, Sandbox
from sandtrap.net.context import allow_network, deny_network, network_allowed
from sandtrap.net.patch import install as install_net


@pytest.fixture(autouse=True)
def _install_net_patches():
    """Ensure network patches are installed for all tests."""
    install_net()


def test_network_blocked_by_default_in_sandbox():
    """Network access is denied during sandbox execution by default."""
    policy = Policy()
    policy.module(socket)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.connect(('127.0.0.1', 1))
finally:
    s.close()
""")
    assert result.error is not None
    assert "Network access denied" in str(result.error)


def test_network_allowed_with_policy_flag():
    """Setting policy.allow_network=True permits network operations."""
    policy = Policy()
    policy.module(socket)
    policy.allow_network = True
    sandbox = Sandbox(policy)
    # Just creating a socket and closing it should succeed
    result = sandbox.exec("""\
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.close()
created = True
""")
    assert result.error is None
    assert result.namespace["created"] is True


def test_network_allowed_for_registered_function():
    """Registered functions with network_access=True can use network."""
    policy = Policy()

    def do_network():
        """Create a socket and close it (proves no StError raised)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return True

    policy.fn(do_network, network_access=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("result = do_network()")
    assert result.error is None
    assert result.namespace["result"] is True


def test_network_allowed_for_module_member():
    """Module members with network_access=True are wrapped."""
    import types

    # Create a module-like object with a network function
    mod = types.ModuleType("netmod")

    def fetch():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return "ok"

    mod.fetch = fetch
    policy = Policy()
    policy.module(mod, name="netmod", network_access=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import netmod
result = netmod.fetch()
""")
    assert result.error is None
    assert result.namespace["result"] == "ok"


def test_af_unix_passes_through():
    """AF_UNIX sockets are not blocked even during denied network context."""
    with deny_network():
        # Creating and closing a UNIX socket should work
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.close()  # No error


def test_getaddrinfo_blocked():
    """socket.getaddrinfo is also blocked during network denial."""
    policy = Policy()
    policy.module(socket)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import socket
socket.getaddrinfo('localhost', 80)
""")
    assert result.error is not None
    assert "Network access denied" in str(result.error)


def test_context_var_isolation():
    """deny_network/allow_network properly restore state."""
    assert network_allowed.get() is True
    with deny_network():
        assert network_allowed.get() is False
        with allow_network():
            assert network_allowed.get() is True
        assert network_allowed.get() is False
    assert network_allowed.get() is True


def test_net_install_idempotent():
    """install() is idempotent -- calling it twice is safe."""
    from sandtrap.net import patch as net_patch

    net_patch.install()
    assert net_patch._installed

    # Second call is a no-op
    net_patch.install()
    assert net_patch._installed


# --- ContextVar propagation to worker threads ---


def test_thread_propagates_deny_network():
    """threading.Thread inherits network_allowed=False from parent."""
    import threading

    results = []

    def worker():
        results.append(network_allowed.get())

    with deny_network():
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert results == [False]


def test_thread_propagates_allow_network():
    """threading.Thread inherits network_allowed=True override from parent."""
    import threading

    results = []

    def worker():
        results.append(network_allowed.get())

    with deny_network():
        with allow_network():
            t = threading.Thread(target=worker)
            t.start()
            t.join()

    assert results == [True]


def test_executor_submit_propagates_deny_network():
    """ThreadPoolExecutor.submit inherits network_allowed=False."""
    from concurrent.futures import ThreadPoolExecutor

    def worker():
        return network_allowed.get()

    with deny_network():
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            assert future.result() is False


def test_executor_submit_propagates_allow_network():
    """ThreadPoolExecutor.submit inherits network_allowed=True override."""
    from concurrent.futures import ThreadPoolExecutor

    def worker():
        return network_allowed.get()

    with deny_network():
        with allow_network():
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                assert future.result() is True


def test_executor_map_propagates_deny_network():
    """ThreadPoolExecutor.map inherits network_allowed=False."""
    from concurrent.futures import ThreadPoolExecutor

    def worker(_):
        return network_allowed.get()

    with deny_network():
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(3)))
    assert results == [False, False, False]


def test_executor_map_propagates_allow_network():
    """ThreadPoolExecutor.map inherits network_allowed=True override."""
    from concurrent.futures import ThreadPoolExecutor

    def worker(_):
        return network_allowed.get()

    with deny_network():
        with allow_network():
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(worker, range(3)))
    assert results == [True, True, True]


def test_thread_network_blocked_in_sandbox():
    """Worker threads in sandbox can't use network when denied."""
    import threading

    policy = Policy()
    policy.module(socket)
    policy.module(threading)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import socket
import threading

errors = []

def worker():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('127.0.0.1', 1))
        s.close()
    except Exception as e:
        errors.append(str(e))

t = threading.Thread(target=worker)
t.start()
t.join()
""")
    assert result.error is None
    assert len(result.namespace["errors"]) == 1
    assert "Network access denied" in result.namespace["errors"][0]


def test_timer_propagates_context():
    """threading.Timer (subclass of Thread) inherits context."""
    import threading

    results = []

    def worker():
        results.append(network_allowed.get())

    with deny_network():
        t = threading.Timer(0, worker)
        t.start()
        t.join()

    assert results == [False]
