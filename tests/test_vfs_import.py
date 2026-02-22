"""Tests for VFS module imports."""

import pickle

from sandtrap import VirtualFS, Policy, Sandbox
from sandtrap.wrappers import ModuleRef, StClass, StFunction


def _make_sandbox(**kwargs):
    """Create a sandbox with a VirtualFS."""
    fs = kwargs.pop("fs", None) or VirtualFS({})
    policy = kwargs.pop("policy", None) or Policy()
    return Sandbox(policy, filesystem=fs, **kwargs), fs


def test_import_vfs_module():
    """Basic VFS module import."""
    sandbox, fs = _make_sandbox()
    fs.write("/helpers.py", b"def double(x): return x * 2")

    result = sandbox.exec("""\
import helpers
result = helpers.double(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 10


def test_from_import_vfs_module():
    """from-import from a VFS module."""
    sandbox, fs = _make_sandbox()
    fs.write("/helpers.py", b"PI = 3.14159\ndef area(r): return PI * r * r")

    result = sandbox.exec("""\
from helpers import area, PI
result = area(2)
""")
    assert result.error is None
    assert abs(result.namespace["result"] - 12.56636) < 0.001
    assert abs(result.namespace["PI"] - 3.14159) < 0.001


def test_from_import_missing_name():
    """from-import of a name that doesn't exist in the VFS module."""
    sandbox, fs = _make_sandbox()
    fs.write("/helpers.py", b"x = 1")

    result = sandbox.exec("from helpers import missing")
    assert result.error is not None
    assert isinstance(result.error, ImportError)
    assert "missing" in str(result.error)


def test_vfs_module_not_found():
    """Import of a non-existent VFS module raises ImportError."""
    sandbox, fs = _make_sandbox()

    result = sandbox.exec("import nonexistent")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_vfs_module_cached():
    """Importing the same VFS module twice returns the cached version."""
    sandbox, fs = _make_sandbox()
    fs.write("/counter.py", b"n = 0")

    result = sandbox.exec("""\
import counter
counter.n = 42
import counter as counter2
result = counter2.n
""")
    assert result.error is None
    assert result.namespace["result"] == 42  # Same module object


def test_vfs_module_sandboxed():
    """VFS module code goes through the sandbox gates."""
    sandbox, fs = _make_sandbox()
    # Module code that tries to access a private attribute should fail
    fs.write("/bad.py", b"""\
class Foo:
    _secret = 42

f = Foo()
val = f._secret
""")
    result = sandbox.exec("import bad")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_vfs_module_uses_checkpoints():
    """VFS module code respects timeouts via checkpoints."""
    policy = Policy()
    policy.timeout = 0.1
    sandbox, fs = _make_sandbox(policy=policy)
    fs.write("/slow.py", b"while True: pass")

    from sandtrap.errors import StTimeout
    result = sandbox.exec("import slow")
    assert result.error is not None
    assert isinstance(result.error, StTimeout)


def test_policy_modules_shadow_vfs():
    """Policy-registered modules take precedence over VFS files."""
    import math

    policy = Policy()
    policy.module(math)
    sandbox, fs = _make_sandbox(policy=policy)
    # Even though math.py exists in VFS, the registered math module wins
    fs.write("/math.py", b"sqrt = lambda x: 'fake'")

    result = sandbox.exec("""\
import math
result = math.sqrt(4)
""")
    assert result.error is None
    assert result.namespace["result"] == 2.0  # Real math, not VFS fake


def test_vfs_module_can_import_registered():
    """VFS modules can import policy-registered modules."""
    import math

    policy = Policy()
    policy.module(math)
    sandbox, fs = _make_sandbox(policy=policy)
    fs.write("/geometry.py", b"""\
import math
def circle_area(r):
    return math.pi * r * r
""")

    result = sandbox.exec("""\
from geometry import circle_area
result = circle_area(1)
""")
    assert result.error is None
    assert abs(result.namespace["result"] - 3.14159265) < 0.001


def test_vfs_module_can_import_vfs_module():
    """VFS modules can import other VFS modules."""
    sandbox, fs = _make_sandbox()
    fs.write("/base.py", b"FACTOR = 10")
    fs.write("/derived.py", b"""\
import base
def scaled(x):
    return x * base.FACTOR
""")

    result = sandbox.exec("""\
from derived import scaled
result = scaled(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 50


def test_vfs_module_classes():
    """VFS modules can define and export classes."""
    sandbox, fs = _make_sandbox()
    fs.write("/models.py", b"""\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def magnitude(self):
        return (self.x ** 2 + self.y ** 2) ** 0.5
""")

    result = sandbox.exec("""\
from models import Point
p = Point(3, 4)
result = p.magnitude()
""")
    assert result.error is None
    assert result.namespace["result"] == 5.0


def test_vfs_module_no_filesystem():
    """Without a filesystem, VFS imports are not attempted."""
    policy = Policy()
    sandbox = Sandbox(policy)  # No filesystem

    result = sandbox.exec("import helpers")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_vfs_circular_import():
    """Circular imports don't crash (partial module returned)."""
    sandbox, fs = _make_sandbox()
    fs.write("/a.py", b"import b\nX = 1")
    fs.write("/b.py", b"import a\nY = 2")

    result = sandbox.exec("""\
import a
import b
result = a.X + b.Y
""")
    assert result.error is None
    assert result.namespace["result"] == 3


def test_vfs_module_syntax_error_not_cached():
    """A VFS module with a syntax error is evicted from cache and can be retried."""
    sandbox, fs = _make_sandbox()
    fs.write("/broken.py", b"def oops(:")

    # First import fails
    result = sandbox.exec("import broken")
    assert result.error is not None
    assert isinstance(result.error, SyntaxError)

    # Fix the module
    fs.write("/broken.py", b"X = 42")

    # Second import should succeed (not return the cached broken module)
    result = sandbox.exec("""\
import broken
result = broken.X
""")
    assert result.error is None
    assert result.namespace["result"] == 42


def test_vfs_module_runtime_error_not_cached():
    """A VFS module that raises at import time is evicted from cache."""
    sandbox, fs = _make_sandbox()
    fs.write("/bad.py", b"raise RuntimeError('init failed')")

    result = sandbox.exec("import bad")
    assert result.error is not None
    assert isinstance(result.error, RuntimeError)

    # Fix the module
    fs.write("/bad.py", b"Y = 99")

    result = sandbox.exec("""\
import bad
result = bad.Y
""")
    assert result.error is None
    assert result.namespace["result"] == 99


# ------------------------------------------------------------------
# Relative imports
# ------------------------------------------------------------------


def test_relative_import_same_package():
    """from .sibling import name works within a VFS package."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/utils.py", b"FACTOR = 7")
    fs.write("/pkg/main.py", b"""\
from .utils import FACTOR
result = FACTOR * 3
""")

    result = sandbox.exec("""\
from pkg import main
result = main.result
""")
    assert result.error is None
    assert result.namespace["result"] == 21


def test_relative_import_dot_only():
    """from . import sibling works to import a sibling module."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/helpers.py", b"X = 42")
    fs.write("/pkg/main.py", b"""\
from . import helpers
result = helpers.X
""")

    result = sandbox.exec("""\
from pkg import main
result = main.result
""")
    assert result.error is None
    assert result.namespace["result"] == 42


def test_relative_import_parent_level():
    """from ..sibling import name goes up one level."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/shared.py", b"VAL = 100")
    fs.write("/pkg/sub/inner.py", b"""\
from ..shared import VAL
doubled = VAL * 2
""")

    result = sandbox.exec("""\
from pkg.sub import inner
result = inner.doubled
""")
    assert result.error is None
    assert result.namespace["result"] == 200


def test_relative_import_no_vfs_fails():
    """Relative imports without a VFS filesystem raise ImportError."""
    sandbox = Sandbox(Policy())
    result = sandbox.exec("from .foo import bar")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_relative_import_from_toplevel():
    """Relative import from top-level sandbox code resolves against VFS root."""
    sandbox, fs = _make_sandbox()
    fs.write("/utils.py", b"X = 99")

    # Top-level sandbox code has no __file__, so base_dir becomes ""
    # and ".utils" resolves to "utils" at VFS root
    result = sandbox.exec("""\
from .utils import X
result = X
""")
    assert result.error is None
    assert result.namespace["result"] == 99


def test_relative_import_dot_only_from_toplevel():
    """from . import mod from top-level resolves to VFS root module."""
    sandbox, fs = _make_sandbox()
    fs.write("/helpers.py", b"Y = 7")

    result = sandbox.exec("""\
from . import helpers
result = helpers.Y
""")
    assert result.error is None
    assert result.namespace["result"] == 7


def test_relative_import_chained():
    """Relative imports work across multiple levels of VFS modules."""
    sandbox, fs = _make_sandbox()
    fs.write("/a/b/c.py", b"VAL = 1")
    fs.write("/a/b/d.py", b"""\
from .c import VAL
DOUBLED = VAL * 2
""")
    fs.write("/a/entry.py", b"""\
from .b.d import DOUBLED
RESULT = DOUBLED + 10
""")

    result = sandbox.exec("""\
from a import entry
result = entry.RESULT
""")
    assert result.error is None
    assert result.namespace["result"] == 12


def test_relative_import_nonexistent_module():
    """Relative import of a nonexistent sibling raises ImportError."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/main.py", b"from .missing import X")

    result = sandbox.exec("from pkg import main")
    assert result.error is not None
    assert isinstance(result.error, ImportError)


def test_relative_import_nonexistent_name():
    """Relative import of a nonexistent name from an existing module."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/utils.py", b"X = 1")
    fs.write("/pkg/main.py", b"from .utils import MISSING")

    result = sandbox.exec("from pkg import main")
    assert result.error is not None
    assert isinstance(result.error, ImportError)
    assert "MISSING" in str(result.error)


# ------------------------------------------------------------------
# VFS wrapped mode wrapping
# ------------------------------------------------------------------


def test_vfs_function_is_sbfunction_in_wrapped_mode():
    """VFS module functions are StFunction in wrapped mode."""
    sandbox, fs = _make_sandbox()
    fs.write("/helpers.py", b"def double(x): return x * 2")

    result = sandbox.exec("""\
from helpers import double
result = double(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 10
    assert isinstance(result.namespace["double"], StFunction)


def test_vfs_function_is_regular_in_raw_mode():
    """VFS module functions are regular functions in raw mode."""
    sandbox, fs = _make_sandbox(mode="raw")
    fs.write("/helpers.py", b"def double(x): return x * 2")

    result = sandbox.exec("""\
from helpers import double
result = double(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 10
    assert not isinstance(result.namespace["double"], StFunction)
    assert callable(result.namespace["double"])


def test_vfs_class_is_sbclass_in_wrapped_mode():
    """VFS module classes are StClass in wrapped mode."""
    sandbox, fs = _make_sandbox()
    fs.write("/models.py", b"""\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
""")

    result = sandbox.exec("""\
from models import Point
p = Point(3, 4)
result = p.x + p.y
""")
    assert result.error is None
    assert result.namespace["result"] == 7
    assert isinstance(result.namespace["Point"], StClass)


def test_vfs_function_pickle_roundtrip():
    """VFS module StFunction survives pickle round-trip."""
    sandbox, fs = _make_sandbox(policy=Policy(tick_limit=10_000))
    fs.write("/helpers.py", b"def double(x): return x * 2")

    result = sandbox.exec("from helpers import double")
    assert result.error is None
    double = result.namespace["double"]

    # Pickle and restore
    data = pickle.dumps(double)
    restored = pickle.loads(data)
    assert isinstance(restored, StFunction)

    # Activate and call
    sandbox.activate(restored)
    assert restored(21) == 42


def test_vfs_module_getattr_gated():
    """getattr() and hasattr() inside VFS modules go through the attribute policy."""

    class Secret:
        _hidden = 42
        public = 99

    policy = Policy(tick_limit=10_000)
    policy.cls(Secret)
    sandbox, fs = _make_sandbox(policy=policy)
    fs.write("/probe.py", b"""\
def read_private(obj):
    return getattr(obj, '_hidden', 'blocked')

def read_public(obj):
    return getattr(obj, 'public', 'missing')

def has_private(obj):
    return hasattr(obj, '_hidden')

def has_public(obj):
    return hasattr(obj, 'public')
""")

    result = sandbox.exec("""\
from probe import read_private, read_public, has_private, has_public
obj = Secret()
pub = read_public(obj)
priv = read_private(obj)
h_pub = has_public(obj)
h_priv = has_private(obj)
""")
    assert result.error is None
    assert result.namespace["pub"] == 99
    assert result.namespace["priv"] == "blocked"
    assert result.namespace["h_pub"] is True
    assert result.namespace["h_priv"] is False


# ------------------------------------------------------------------
# ModuleRef reactivation
# ------------------------------------------------------------------


def test_moduleref_bare_dotted_import_reactivation():
    """ModuleRef for a bare dotted import (import pkg.mod) restores the package chain."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/__init__.py", b"")
    fs.write("/pkg/mod.py", b"X = 42")

    # Simulate what happens after deserializing a namespace that had
    # ``import pkg.mod`` — the key is "pkg" and the ModuleRef stores "pkg.mod"
    ns = {"pkg": ModuleRef("pkg.mod")}
    result = sandbox.exec("result = pkg.mod.X", namespace=ns)
    assert result.error is None
    assert result.namespace["result"] == 42


def test_moduleref_aliased_dotted_import_reactivation():
    """ModuleRef for an aliased dotted import (import pkg.mod as m) returns the leaf."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/__init__.py", b"")
    fs.write("/pkg/mod.py", b"Y = 99")

    # Simulate ``import pkg.mod as m`` — key is "m", ModuleRef stores "pkg.mod"
    ns = {"m": ModuleRef("pkg.mod")}
    result = sandbox.exec("result = m.Y", namespace=ns)
    assert result.error is None
    assert result.namespace["result"] == 99


def test_moduleref_simple_import_reactivation():
    """ModuleRef for a simple import (import helpers) resolves correctly."""
    sandbox, fs = _make_sandbox()
    fs.write("/helpers.py", b"Z = 7")

    ns = {"helpers": ModuleRef("helpers")}
    result = sandbox.exec("result = helpers.Z", namespace=ns)
    assert result.error is None
    assert result.namespace["result"] == 7


# ------------------------------------------------------------------
# VFS package __init__.py execution
# ------------------------------------------------------------------


def test_vfs_package_init_executes():
    """__init__.py in a VFS package is executed during dotted import."""
    sandbox, fs = _make_sandbox()
    fs.write("/pkg/__init__.py", b"PKG_LOADED = True")
    fs.write("/pkg/mod.py", b"VAL = 1")

    result = sandbox.exec("""\
import pkg.mod
result = pkg.PKG_LOADED
""")
    assert result.error is None
    assert result.namespace["result"] is True
