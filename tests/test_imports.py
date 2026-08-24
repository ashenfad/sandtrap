"""Tests for import gates (Phase 3)."""

import importlib
import math
import sys
import types

import pytest

from sandtrap import Policy, Sandbox
from sandtrap.errors import StCancelled, StTickLimit, StTimeout, StValidationError


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


def test_from_import_lazy_submodule():
    """``from PIL import ImageDraw`` should work when ``PIL`` is
    registered with ``recursive=True``, even when ``PIL.ImageDraw``
    hasn't been pre-loaded as an attribute on the parent package.

    Namespace-package-style modules (``PIL``, ``email``, ``urllib``,
    etc.) don't eager-import their submodules in ``__init__.py``,
    so ``hasattr(PIL, "ImageDraw")`` is ``False`` until something
    has explicitly imported it.  ``policy.resolve_module`` already
    falls back to ``importlib.import_module`` in this case (path
    used by ``import PIL.ImageDraw``); ``policy.resolve_module_member``
    (path used by ``from PIL import ImageDraw``) must do the same
    or this asymmetry leaves agents stuck on the from-form.
    """
    PIL = pytest.importorskip("PIL")

    # Defensively unload PIL.ImageDraw and detach it from the parent
    # so the fresh-load condition is reproducible regardless of
    # whether earlier tests / imports have already pulled it in.
    sys.modules.pop("PIL.ImageDraw", None)
    if hasattr(PIL, "ImageDraw"):
        delattr(PIL, "ImageDraw")
    assert not hasattr(PIL, "ImageDraw"), (
        "test setup failed: PIL.ImageDraw should not be a parent attr"
    )

    policy = Policy()
    policy.module(PIL, recursive=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from PIL import ImageDraw\ndraw_cls = ImageDraw.Draw")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["draw_cls"] is PIL.ImageDraw.Draw


def test_from_import_submodule_blocked_when_non_recursive_eager():
    """``from os import path`` should be blocked when ``os`` is
    registered with ``recursive=False`` — even though ``os.path`` is
    eager-loaded as a parent attribute at ``import os`` time.

    Without this gate, agents bypass the ``recursive=False`` posture
    entirely for any submodule that the parent package happens to
    pre-import in its ``__init__.py`` (``os.path``, ``email.mime``,
    ``urllib.parse``, etc.).  Submodule access goes through the
    import policy, the same as ``import os.path`` would.
    """
    import os

    policy = Policy()
    policy.module(os)  # recursive=False (default)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from os import path")
    assert isinstance(result.error, ImportError), (
        f"expected ImportError, got: {result.error!r}"
    )


def test_from_import_submodule_blocked_when_non_recursive_lazy():
    """Same gate as the eager case, exercised through the lazy
    fallback path: a non-recursive registration must not let agents
    pull in arbitrary submodules via ``importlib.import_module``."""
    PIL = pytest.importorskip("PIL")

    sys.modules.pop("PIL.ImageDraw", None)
    if hasattr(PIL, "ImageDraw"):
        delattr(PIL, "ImageDraw")

    policy = Policy()
    policy.module(PIL)  # recursive=False (default)
    sandbox = Sandbox(policy)
    result = sandbox.exec("from PIL import ImageDraw")
    assert isinstance(result.error, ImportError), (
        f"expected ImportError, got: {result.error!r}"
    )


def test_from_import_regular_member_unaffected_by_submodule_gate():
    """The submodule gate must not interfere with regular member
    imports (functions, constants).  ``from os import getcwd``
    should still work against a non-recursive ``os`` registration —
    ``getcwd`` is a function, not a submodule, so the policy posture
    on ``os`` itself is what governs."""
    import os

    policy = Policy()
    policy.module(os)  # recursive=False
    sandbox = Sandbox(policy)
    result = sandbox.exec("from os import getcwd\ncwd = getcwd()")
    assert result.error is None, f"unexpected error: {result.error}"
    assert isinstance(result.namespace["cwd"], str)


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


# ---- dynamic __import__ ----
#
# Source-level ``__import__`` is rewritten to the policy-gated
# __st_dynimport__ (Rewriter.visit_Name).  It reaches the same policy check
# as an ``import`` statement, and stays separate from the real __import__
# that lives in builtins for C extensions -- the statement resolves that one
# from the frame's builtins, a name load never does.


def test_dynamic_import_granted_module():
    """__import__('math') resolves like `import math`."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("m = __import__('math')\nx = m.sqrt(16)")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == 4.0


def test_dynamic_import_computed_name():
    """The whole point: the module name need not be a literal."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("name = 'ma' + 'th'\nx = __import__(name).pi")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == math.pi


def test_dynamic_import_ungranted_module_blocked():
    """An ungranted module is refused, same as the statement form."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("m = __import__('os')")
    assert isinstance(result.error, ImportError)
    assert "os" in str(result.error)


def test_dynamic_import_reports_the_users_line():
    """The gate's extra frame must not swallow the real line number."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = 1\ny = 2\nm = __import__('os')")
    assert isinstance(result.error, ImportError)
    assert "(line 3)" in str(result.error)


def test_dynamic_import_alias_is_the_gate():
    """`f = __import__` binds the gate, so calls through it stay gated."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    ok = sandbox.exec("f = __import__\nx = f('math').tau")
    assert ok.error is None, f"unexpected error: {ok.error}"
    assert ok.namespace["x"] == math.tau

    blocked = sandbox.exec("f = __import__\nx = f('os')")
    assert isinstance(blocked.error, ImportError)


def test_dynamic_import_cannot_be_rebound_or_deleted():
    """Rebinding would let the name fall through to the real builtin."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    for src in ("__import__ = None", "del __import__", "global __import__"):
        result = sandbox.exec(src)
        assert isinstance(result.error, StValidationError), src


def test_dynamic_import_works_inside_a_function():
    """LOAD_GLOBAL from a function body resolves the gate too."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("def f():\n    return __import__('math').e\nx = f()")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == math.e


def test_dynamic_import_dotted_returns_top_level():
    """__import__('os.path') returns `os`, matching CPython."""
    import os

    policy = Policy()
    policy.module(os, recursive=True)
    sandbox = Sandbox(policy)
    # `os` has a `path` attribute; `os.path` does not -- so a successful
    # m.path.join proves the top-level package came back, not the leaf.
    result = sandbox.exec("m = __import__('os.path')\nx = m.path.join('a', 'b')")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == "a/b"


def test_dynamic_import_fromlist_returns_the_submodule():
    """A non-empty fromlist selects the leaf module, matching CPython."""
    import os

    policy = Policy()
    policy.module(os, recursive=True)
    sandbox = Sandbox(policy)
    result = sandbox.exec(
        "m = __import__('os.path', fromlist=['join'])\nx = m.join('a', 'b')"
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == "a/b"


def test_dynamic_import_fromlist_does_not_widen_policy():
    """fromlist can't reach a member the policy filters out."""
    policy = Policy()
    policy.module(math, include=("sqrt",))
    sandbox = Sandbox(policy)
    ok = sandbox.exec("x = __import__('math', fromlist=['sqrt']).sqrt(9)")
    assert ok.error is None, f"unexpected error: {ok.error}"
    assert ok.namespace["x"] == 3.0

    blocked = sandbox.exec("x = __import__('math', fromlist=['pow']).pow(2, 3)")
    assert blocked.error is not None
    assert isinstance(blocked.error, AttributeError)


def test_dynamic_import_relative_is_refused():
    """level > 0 has no well-defined caller context here; say so clearly."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("m = __import__('math', level=1)")
    assert isinstance(result.error, ImportError)
    assert "level > 0" in str(result.error)


def test_dynamic_import_of_synthetic_sys():
    """The synthetic `sys` is reachable dynamically, like `import sys`."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("s = __import__('sys')\nx = s.argv", argv=["prog", "a"])
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == ["prog", "a"]


def test_dynamic_import_gate_does_not_leak_into_namespace():
    """The gate is internal; it must not show up in result.namespace."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("m = __import__('math')")
    assert result.error is None
    assert not [k for k in result.namespace if "import" in k.lower()]


def test_real_import_still_unreachable_via_builtins():
    """The C-extension __import__ must stay out of reach."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    for src in ("x = __builtins__['__import__']", "x = __builtins__.__import__"):
        result = sandbox.exec(src)
        assert isinstance(result.error, StValidationError), src
        assert "__builtins__" in str(result.error)


@pytest.mark.parametrize("exc", [StTimeout, StCancelled, StTickLimit])
def test_dynamic_import_fromlist_does_not_swallow_control_flow(exc):
    """The fromlist prefetch swallows a missing name, but must not eat a
    timeout/cancellation raised while resolving one.

    StTimeout/StCancelled/StTickLimit derive from StError(Exception), so a
    blanket `except Exception` around the prefetch would silently defeat
    timeouts and cancellation.
    """

    class _Boom(types.ModuleType):
        def __getattr__(self, attr):
            # Only the fromlist entry detonates -- anything else must behave
            # like a normal missing attribute so sandbox setup can probe it.
            if attr == "boom":
                raise exc("raised during fromlist resolution")
            raise AttributeError(attr)

    policy = Policy()
    policy.module(_Boom("boomy"))
    sandbox = Sandbox(policy)
    result = sandbox.exec("m = __import__('boomy', fromlist=['boom'])")
    assert isinstance(result.error, exc), f"got {result.error!r}"


def test_dynamic_import_fromlist_still_swallows_a_missing_name():
    """The CPython behaviour the narrow except preserves."""
    policy = Policy()
    policy.module(math)
    sandbox = Sandbox(policy)
    result = sandbox.exec("m = __import__('math', fromlist=['no_such_thing'])")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["m"] is math


def _write_pkg(tmp_path, monkeypatch):
    """A real on-disk package with a lazily-loaded good and broken submodule."""
    pkg = tmp_path / "lazypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")  # deliberately no eager submodule imports
    (pkg / "fine.py").write_text("VALUE = 7")
    (pkg / "broken.py").write_text("import totally_missing_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module("lazypkg")


def test_dynamic_import_fromlist_binds_a_lazy_submodule(tmp_path, monkeypatch):
    """The prefetch is what makes PIL-style lazy submodules reachable."""
    pkg = _write_pkg(tmp_path, monkeypatch)
    policy = Policy()
    policy.module(pkg, recursive=True)
    sandbox = Sandbox(policy)

    result = sandbox.exec(
        "m = __import__('lazypkg', fromlist=['fine'])\nx = m.fine.VALUE"
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["x"] == 7


def test_dynamic_import_fromlist_propagates_a_submodule_load_failure(
    tmp_path, monkeypatch
):
    """A submodule that exists but fails to load must not read as 'absent'.

    Swallowing it would hand back the parent module as though nothing went
    wrong, and the real cause would resurface much later as a confusing
    AttributeError -- where the statement form reports it immediately.
    """
    pkg = _write_pkg(tmp_path, monkeypatch)
    policy = Policy()
    policy.module(pkg, recursive=True)
    sandbox = Sandbox(policy)

    statement = sandbox.exec("from lazypkg import broken")
    dynamic = sandbox.exec("m = __import__('lazypkg', fromlist=['broken'])")
    assert isinstance(statement.error, ImportError)
    assert isinstance(dynamic.error, ImportError), (
        f"load failure was swallowed: {dynamic.error!r}"
    )
    assert str(dynamic.error) == str(statement.error)


def test_dynamic_import_fromlist_dummy_idiom_still_tolerated(tmp_path, monkeypatch):
    """`__import__(m, fromlist=['dummy'])` is the standard 'give me the leaf'
    idiom -- an absent entry must stay tolerated."""
    pkg = _write_pkg(tmp_path, monkeypatch)
    policy = Policy()
    policy.module(pkg, recursive=True)
    sandbox = Sandbox(policy)

    result = sandbox.exec("m = __import__('lazypkg', fromlist=['dummy'])")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["m"] is pkg
