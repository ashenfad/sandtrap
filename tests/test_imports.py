"""Tests for import gates (Phase 3)."""

import math

import pytest

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
    result = sandbox.exec(
        "from math import sqrt, cos, pi\nx = sqrt(4)\ny = cos(0)\nz = pi"
    )
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


def test_builtins_access_blocked():
    """User code cannot access __builtins__ to extract __import__."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
imp = __builtins__["__import__"]
""")
    assert result.error is not None
    assert isinstance(result.error, StValidationError)
    assert "__builtins__" in str(result.error)


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


def test_pandas_astype_str():
    """pandas .astype(str) works when builtin types are real.

    When builtin types were wrapped with _GatedMeta proxies for checkpoint
    gating, pandas' .astype(str) failed because np.dtype() didn't recognize
    the proxy as the real str type.
    """
    pd = pytest.importorskip("pandas")

    policy = Policy()
    policy.module(pd, recursive=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import pandas as pd
s = pd.Series([1, 2, 3])
result = list(s.astype(str))
""")
    assert result.error is None, f"astype(str) failed: {result.error}"
    assert result.namespace["result"] == ["1", "2", "3"]


def test_registered_module_transitive_stdlib_import():
    """Library-internal imports of unregistered stdlib modules should not be blocked.

    When a registered module (e.g. pandas) internally imports a stdlib module
    (e.g. time) via C-level PyObject_GetAttr(builtins, "__import__"), the
    sandbox's policy-gated __import__ intercepts the call and blocks it because
    the stdlib module isn't registered.  This is a bug — the import gate should
    only restrict exec'd user code, not library internals.
    """
    pd = pytest.importorskip("pandas")

    policy = Policy()
    policy.module(pd, recursive=True)
    sandbox = Sandbox(policy)

    # pd.Timestamp.strftime internally imports 'time' (via C-level __import__).
    # This should succeed because it's a library-internal import, not user code.
    result = sandbox.exec("""\
import pandas as pd
ts = pd.Timestamp('2024-01-15')
result = ts.strftime('%B %Y')
""")
    assert result.error is None, f"Library-internal import blocked: {result.error}"
    assert result.namespace["result"] == "January 2024"


def test_builtins_import_not_accessible_via_getattr():
    """Sandboxed code cannot access __builtins__ at all.

    The AST rewriter blocks __builtins__ as a name in Load context,
    preventing both item access (__builtins__["__import__"]) and
    attribute access (__builtins__.__import__).
    """
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
x = __builtins__.__import__
""")
    assert result.error is not None
    assert isinstance(result.error, StValidationError)
    assert "__builtins__" in str(result.error)


# ---- "from main import X" fallback ----


def test_from_main_import_resolves_sandbox_globals():
    """'from main import X' resolves X from the sandbox namespace."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
Response = 'I am Response'
from main import Response as R
x = R
""")
    assert result.error is None
    assert result.namespace["x"] == "I am Response"


def test_from_dunder_main_import_resolves_sandbox_globals():
    """'from __main__ import X' also resolves from the sandbox namespace."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
from __main__ import math as m
x = m.sqrt(16)
""")
    assert result.error is None
    assert result.namespace["x"] == 4.0


def test_from_main_import_missing_name_still_errors():
    """'from main import X' raises ImportError when X is not in namespace."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("from main import NoSuchThing")
    assert result.error is not None
    assert isinstance(result.error, ImportError)
