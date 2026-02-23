"""Tests for import gates (Phase 3)."""

import math

from sandtrap import Policy, Sandbox
from sandtrap.errors import StValidationError


def test_import_allowed_module():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("import math\nx = math.sqrt(16)")
    assert result.error is None
    assert result.namespace["x"] == 4.0


def test_import_blocked_module():
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("import os")
    assert result.error is not None
    assert isinstance(result.error, ImportError)
    assert "not allowed" in str(result.error)


def test_import_with_alias():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("import math as m\nx = m.ceil(3.2)")
    assert result.error is None
    assert result.namespace["x"] == 4


def test_from_import():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from math import sqrt\nx = sqrt(25)")
    assert result.error is None
    assert result.namespace["x"] == 5.0


def test_from_import_with_alias():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from math import sqrt as square_root\nx = square_root(9)")
    assert result.error is None
    assert result.namespace["x"] == 3.0


def test_from_import_multiple():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from math import sqrt, cos, pi\nx = sqrt(4)\ny = cos(0)\nz = pi")
    assert result.error is None
    assert result.namespace["x"] == 2.0
    assert result.namespace["y"] == 1.0
    assert result.namespace["z"] == math.pi


def test_from_import_blocked_module():
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("from os import path")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_from_import_filtered_member():
    policy = Policy()
    policy.module(math, include=["sqrt", "ceil"])
    sandbox = Sandbox(policy)
    # sqrt should work
    result = sandbox.exec("from math import sqrt\nx = sqrt(4)")
    assert result.error is None
    assert result.namespace["x"] == 2.0
    # cos should be blocked
    result = sandbox.exec("from math import cos")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_from_import_excluded_private():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from math import _private_fn")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_wildcard_import_blocked():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from math import *")
    assert isinstance(result.error, StValidationError)
    assert "Wildcard imports" in str(result.error)


def test_registered_function_in_namespace():
    policy = Policy()
    policy.fn(math.sqrt, name="sqrt")
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = sqrt(16)")
    assert result.error is None
    assert result.namespace["x"] == 4.0


def test_registered_class_in_namespace():
    policy = Policy()

    class MyClass:
        def __init__(self, val):
            self.val = val
        def double(self):
            return self.val * 2

    policy.cls(MyClass)
    sandbox = Sandbox(policy)
    result = sandbox.exec("obj = MyClass(5)\nresult = obj.double()")
    assert result.error is None
    assert result.namespace["result"] == 10


def test_import_multiple_modules():
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    # import math, math — both should resolve
    result = sandbox.exec("import math\nx = math.sqrt(4)")
    assert result.error is None
    assert result.namespace["x"] == 2.0


def test_end_to_end_module_and_print():
    """Full pipeline: register module, import, use, print."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import math
x = math.sqrt(16)
print(x)
""")
    assert result.error is None
    assert result.namespace["x"] == 4.0
    assert result.stdout == "4.0\n"


def test_recursive_module_import():
    """Recursive module registration allows submodule imports."""
    import json

    policy = Policy()
    policy.module(json, recursive=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("import json\nx = json.dumps([1, 2, 3])")
    assert result.error is None
    assert result.namespace["x"] == "[1, 2, 3]"


# ---- C-level __import__ tests ----
#
# C extensions (e.g. numpy) call PyObject_GetAttr(builtins, "__import__")
# to import submodules internally.  The sandbox provides a policy-gated
# __import__ so these work for registered modules but remain blocked for
# unregistered ones.


def test_clevel_import_allowed_for_registered_module():
    """C-level __import__ succeeds for a policy-registered module."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    # Simulate what C code does: builtins.__import__("math")
    result = sandbox.exec("""\
imp = __builtins__["__import__"]
m = imp("math")
x = m.sqrt(16)
""")
    assert result.error is None
    assert result.namespace["x"] == 4.0


def test_clevel_import_blocked_for_unregistered_module():
    """C-level __import__ rejects modules not in the policy."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
imp = __builtins__["__import__"]
m = imp("os")
""")
    assert result.error is not None
    assert isinstance(result.error, ImportError)
    assert "not allowed" in str(result.error)


def test_clevel_import_recursive_submodule():
    """C-level __import__ allows submodules of a recursive registration."""
    import json
    import json.decoder  # ensure loaded

    policy = Policy()
    policy.module(json, recursive=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
imp = __builtins__["__import__"]
dec = imp("json.decoder", fromlist=["JSONDecodeError"])
x = hasattr(dec, "JSONDecodeError")
""")
    assert result.error is None
    assert result.namespace["x"] is True


def test_clevel_import_recursive_blocks_unrelated():
    """Recursive registration for one module doesn't open others."""
    import json

    policy = Policy()
    policy.module(json, recursive=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
imp = __builtins__["__import__"]
m = imp("subprocess")
""")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_clevel_import_relative_passthrough():
    """C-level __import__ delegates relative imports to real __import__."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    # level > 0 is passed through to real __import__ (which will fail
    # here because there's no package context, but it's not our gate
    # that rejects it)
    result = sandbox.exec("""\
imp = __builtins__["__import__"]
try:
    m = imp("math", level=1)
    failed = False
except (ImportError, KeyError, TypeError):
    failed = True
""")
    assert result.error is None
    assert result.namespace["failed"] is True


def test_clevel_import_numpy_operations():
    """Numpy C-level internal imports work when numpy is registered."""
    numpy = None
    try:
        import numpy
    except ImportError:
        pass
    if numpy is None:
        import pytest

        pytest.skip("numpy not installed")

    policy = Policy()
    policy.module(numpy, recursive=True)
    sandbox = Sandbox(policy)
    # Operations that trigger C-level submodule imports:
    # .astype(int), .dtype access, f-string formatting of numpy scalars
    result = sandbox.exec("""\
import numpy as np
arr = np.arange(12)
arr2 = arr.astype(float)
d = arr.dtype
s = arr.sum()
desc = f"dtype={d}, sum={s}"
""")
    assert result.error is None, f"Unexpected error: {result.error}"
    assert result.namespace["desc"] == "dtype=int64, sum=66"


def test_builtins_import_not_accessible_via_getattr():
    """Sandboxed code cannot access __import__ via attribute access.

    The AST rewriter transforms obj.attr to __st_getattr__(obj, attr),
    and __import__ is not in DEFAULT_ALLOWED_DUNDERS, so attribute-style
    access is blocked.  Only item access (__builtins__["__import__"])
    works, which is the path C-level code uses.
    """
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
x = __builtins__.__import__
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)
