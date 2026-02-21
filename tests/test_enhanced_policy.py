"""Tests for enhanced policy resolution and error handling (Phase 5)."""

import math
import traceback

from sandtrap import Policy, Sandbox


class Robot:
    """Test class for policy filtering."""

    def __init__(self, name):
        self.name = name
        self._id = 42

    def greet(self):
        return f"Hello, I'm {self.name}"

    def move(self, x, y):
        return (x, y)

    def _internal(self):
        return "secret"

    def shutdown(self):
        return "shutdown"


def test_class_include_filter():
    policy = Policy()
    policy.cls(Robot, include=["greet", "move"])
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
r = Robot('Hal')
msg = r.greet()
pos = r.move(1, 2)
""")
    assert result.error is None
    assert result.namespace["msg"] == "Hello, I'm Hal"
    assert result.namespace["pos"] == (1, 2)


def test_class_exclude_filter():
    policy = Policy()
    policy.cls(Robot, exclude=["shutdown", "_*"])
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
r = Robot('Hal')
r.shutdown()
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_class_include_blocks_unlisted():
    policy = Policy()
    policy.cls(Robot, include=["greet"])
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
r = Robot('Hal')
r.move(1, 2)
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_module_include_filter_attr_access():
    policy = Policy()
    policy.module(math, include=["sqrt", "pi"])
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
import math
x = math.sqrt(4)
y = math.pi
""")
    assert result.error is None
    assert result.namespace["x"] == 2.0

    # cos should be blocked via attr access
    result = sandbox.exec("""\
import math
math.cos(0)
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_module_exclude_filter_attr_access():
    policy = Policy()
    policy.module(math, exclude=["cos", "_*", "*._*"])
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
import math
x = math.sqrt(4)
""")
    assert result.error is None

    result = sandbox.exec("""\
import math
math.cos(0)
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_traceback_shows_user_code():
    """Error tracebacks should reference user source, not sandtrap internals."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = 1\ny = 1/0\nz = 3")
    assert result.error is not None

    tb_text = "".join(
        traceback.format_exception(
            type(result.error), result.error, result.error.__traceback__
        )
    )
    assert "<sandtrap:" in tb_text


def test_error_no_sb_names():
    """Error messages should not expose __st_* gate names."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
class Obj:
    pass
o = Obj()
o._private = 1
""")
    assert result.error is not None
    msg = str(result.error)
    assert "__st_" not in msg


def test_class_default_private_blocked():
    """Default policy blocks _private on registered class instances too."""
    policy = Policy()
    policy.cls(Robot)
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
r = Robot('Hal')
x = r._internal()
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_unregistered_object_uses_defaults():
    """Objects of unregistered types use default attr rules."""
    policy = Policy()
    sandbox = Sandbox(policy)

    result = sandbox.exec("""\
class Foo:
    x = 1
    _y = 2

f = Foo()
a = f.x
""")
    assert result.error is None
    assert result.namespace["a"] == 1

    result = sandbox.exec("""\
class Foo:
    _y = 2
f = Foo()
b = f._y
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_super_respects_include_filter():
    """super() on a registered class respects include filters."""

    class Base:
        def allowed(self):
            return "ok"

        def blocked(self):
            return "secret"

    policy = Policy()
    policy.cls(Base, include="allowed")
    sandbox = Sandbox(policy, mode="raw")

    # Direct access to allowed method works
    result = sandbox.exec(
        "result = obj.allowed()",
        namespace={"obj": Base()},
    )
    assert result.error is None
    assert result.namespace["result"] == "ok"

    # Direct access to blocked method fails
    result = sandbox.exec(
        "result = obj.blocked()",
        namespace={"obj": Base()},
    )
    assert result.error is not None
    assert isinstance(result.error, AttributeError)

    # super() in a subclass also respects the filter
    result = sandbox.exec("""\
class Child(Base):
    def try_allowed(self):
        return super().allowed()
    def try_blocked(self):
        return super().blocked()

c = Child()
result = c.try_allowed()
""", namespace={"Base": Base})
    assert result.error is None
    assert result.namespace["result"] == "ok"

    result = sandbox.exec("""\
class Child(Base):
    def try_blocked(self):
        return super().blocked()

c = Child()
result = c.try_blocked()
""", namespace={"Base": Base})
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


# --- Submodule access with recursive flag ---


def test_non_recursive_module_blocks_submodule_attribute():
    """Non-recursive module registration blocks submodule access via attributes."""
    import os

    policy = Policy()
    policy.module(os, recursive=False)
    sandbox = Sandbox(policy)

    result = sandbox.exec("import os\nx = os.path.join('a', 'b')")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)
    assert "path" in str(result.error)


def test_recursive_module_allows_submodule_attribute():
    """Recursive module registration allows submodule access via attributes."""
    import os

    policy = Policy()
    policy.module(os, recursive=True)
    sandbox = Sandbox(policy)

    result = sandbox.exec("import os\nx = os.path.join('a', 'b')")
    assert result.error is None
    assert result.namespace["x"] == os.path.join("a", "b")


def test_non_recursive_with_separate_submodule_registration():
    """Non-recursive parent + separately registered submodule allows access."""
    import os
    import os.path

    policy = Policy()
    policy.module(os, recursive=False)
    policy.module(os.path, name="os.path")
    sandbox = Sandbox(policy)

    result = sandbox.exec("import os\nx = os.path.join('a', 'b')")
    assert result.error is None
    assert result.namespace["x"] == os.path.join("a", "b")
