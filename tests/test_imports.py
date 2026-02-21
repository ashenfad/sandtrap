"""Tests for import gates (Phase 3)."""

import math

from sandtrap import Policy, Sandbox
from sandtrap.errors import SbValidationError


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
    assert isinstance(result.error, SbValidationError)
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
