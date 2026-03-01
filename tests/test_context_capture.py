"""Tests for raw mode ContextVar capture on functions and lambdas."""

import asyncio

from sandtrap import Policy, Sandbox
from sandtrap.fs import VirtualFS
from sandtrap.net.context import network_allowed


def _make_vfs(files: dict[str, str] | None = None) -> VirtualFS:
    """Create a VirtualFS with optional text files."""
    vfs = VirtualFS()
    if files:
        for path, content in files.items():
            vfs.write(path, content.encode())
    return vfs


# -- Sync functions ----------------------------------------------------------


def test_raw_function_restores_filesystem():
    """A raw mode function called outside exec should restore current_fs."""
    vfs = _make_vfs({"/data.txt": "hello"})
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
def read_data():
    with open('/data.txt') as f:
        return f.read()
""")
    assert result.error is None

    # Call outside exec — ContextVar should be restored by wrapper
    read_data = result.namespace["read_data"]
    assert read_data() == "hello"


def test_raw_function_restores_network_denial():
    """A raw mode function called outside exec should restore network_allowed."""
    policy = Policy(timeout=5.0, allow_network=False)
    sb = Sandbox(policy, mode="raw")

    # The function captures a host-provided list and appends the network state.
    # This avoids needing to import contextvars inside the sandbox.
    observed = []

    result = sb.exec(
        """\
def check_network():
    _observed.append(_network_allowed.get())
""",
        namespace={"_observed": observed, "_network_allowed": network_allowed},
    )
    assert result.error is None

    # Outside exec, network_allowed defaults to True
    assert network_allowed.get() is True

    # Call the captured function — wrapper should restore False
    check = result.namespace["check_network"]
    check()
    assert observed == [False]

    # After the call, the ContextVar should be reset
    assert network_allowed.get() is True


def test_raw_lambda_restores_filesystem():
    """A raw mode lambda called outside exec should restore current_fs."""
    vfs = _make_vfs({"/data.txt": "lambda-test"})
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
reader = lambda: open('/data.txt').read()
""")
    assert result.error is None

    reader = result.namespace["reader"]
    assert reader() == "lambda-test"


def test_raw_inner_function_captures_context():
    """Inner functions (closures) should also capture context."""
    vfs = _make_vfs({"/data.txt": "inner-test"})
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
def make_reader():
    def reader():
        with open('/data.txt') as f:
            return f.read()
    return reader

my_reader = make_reader()
""")
    assert result.error is None

    my_reader = result.namespace["my_reader"]
    assert my_reader() == "inner-test"


# -- Async functions ----------------------------------------------------------


def test_raw_async_function_restores_filesystem():
    """An async raw mode function should restore current_fs when awaited."""
    vfs = _make_vfs({"/data.txt": "async-test"})
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
async def read_data():
    with open('/data.txt') as f:
        return f.read()
""")
    assert result.error is None

    read_data = result.namespace["read_data"]
    assert asyncio.get_event_loop().run_until_complete(read_data()) == "async-test"


# -- Decorated functions ------------------------------------------------------


def test_decorated_function_preserves_api():
    """Context capture should not break decorator APIs (like .some_attr)."""
    vfs = _make_vfs({"/data.txt": "deco-test"})
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
def my_decorator(fn):
    fn.custom_attr = 'preserved'
    fn.refresh = lambda: 'refreshed'
    return fn

@my_decorator
def read_data():
    with open('/data.txt') as f:
        return f.read()
""")
    assert result.error is None

    read_data = result.namespace["read_data"]
    # Decorator API should be on the outermost object
    assert read_data.custom_attr == "preserved"
    assert read_data.refresh() == "refreshed"
    # And the function should still work with ContextVars restored
    assert read_data() == "deco-test"


# -- Class methods ------------------------------------------------------------


def test_raw_class_method_restores_context():
    """Class methods used as callbacks should capture context."""
    vfs = _make_vfs({"/config.txt": "class-test"})
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
class App:
    def load(self):
        with open('/config.txt') as f:
            return f.read()

app = App()
""")
    assert result.error is None

    app = result.namespace["app"]
    assert app.load() == "class-test"


# -- No-op when no restrictions -----------------------------------------------


def test_no_wrapping_when_no_restrictions():
    """When no filesystem and network is allowed, functions should not be wrapped."""
    policy = Policy(timeout=5.0, allow_network=True)
    sb = Sandbox(policy, mode="raw")  # No filesystem

    result = sb.exec("""\
def identity(x):
    return x
""")
    assert result.error is None

    fn = result.namespace["identity"]
    # The function should be unwrapped (no __wrapped__ attribute)
    assert not hasattr(fn, "__wrapped__")
    assert fn(42) == 42


def test_wrapping_present_when_filesystem_active():
    """When filesystem is active, functions should be wrapped."""
    vfs = _make_vfs()
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="raw", filesystem=vfs)

    result = sb.exec("""\
def identity(x):
    return x
""")
    assert result.error is None

    fn = result.namespace["identity"]
    # functools.wraps sets __wrapped__
    assert hasattr(fn, "__wrapped__")
    assert fn(42) == 42


# -- Wrapped mode unaffected --------------------------------------------------


def test_wrapped_mode_not_affected():
    """Wrapped mode should continue to use StFunction, not context capture."""
    vfs = _make_vfs()
    policy = Policy(timeout=5.0)
    sb = Sandbox(policy, mode="wrapped", filesystem=vfs)

    result = sb.exec("""\
def identity(x):
    return x
""")
    assert result.error is None

    fn = result.namespace["identity"]
    # In wrapped mode, functions are StFunction instances
    from sandtrap.wrappers import StFunction

    assert isinstance(fn, StFunction)
