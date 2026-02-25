"""Tests for the Sandbox execution pipeline."""

import sys

import pytest

from sandtrap import Policy, Sandbox
from sandtrap.errors import StValidationError


@pytest.fixture
def sandbox():
    return Sandbox(Policy())


def test_simple_arithmetic(sandbox):
    result = sandbox.exec("x = 2 + 3")
    assert result.error is None
    assert result.namespace["x"] == 5


def test_print_capture(sandbox):
    result = sandbox.exec("print('hello')\nprint('world')")
    assert result.error is None
    assert result.stdout == "hello\nworld\n"


def test_print_sep_end(sandbox):
    result = sandbox.exec("print(1, 2, 3, sep=', ', end='!')")
    assert result.error is None
    assert result.stdout == "1, 2, 3!"


def test_builtins_available(sandbox):
    result = sandbox.exec("x = len([1, 2, 3])\ny = abs(-5)\nz = sorted([3, 1, 2])")
    assert result.error is None
    assert result.namespace["x"] == 3
    assert result.namespace["y"] == 5
    assert result.namespace["z"] == [1, 2, 3]


def test_builtin_types(sandbox):
    result = sandbox.exec("x = int('42')\ny = str(3.14)\nz = list(range(3))")
    assert result.error is None
    assert result.namespace["x"] == 42
    assert result.namespace["y"] == "3.14"
    assert result.namespace["z"] == [0, 1, 2]


def test_builtin_types_are_real():
    """Builtin types in the sandbox are real types, not proxies.

    Library code that receives builtin types (e.g. df.astype(str),
    np.dtype(int)) needs real types for identity checks at the C level.
    """
    policy = Policy(timeout=5.0)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
results = {
    'str_is_str': str is str,
    'int_is_int': int is int,
    'float_is_float': float is float,
    'dict_is_dict': dict is dict,
    'list_is_list': list is list,
    'isinstance_works': isinstance(42, int),
    'issubclass_works': issubclass(bool, int),
}
""")
    assert result.error is None
    for check, passed in result.namespace["results"].items():
        assert passed, f"{check} failed"


def test_control_flow(sandbox):
    result = sandbox.exec("""\
total = 0
for i in range(5):
    if i % 2 == 0:
        total += i
""")
    assert result.error is None
    assert result.namespace["total"] == 6  # 0 + 2 + 4


def test_function_def_and_call(sandbox):
    result = sandbox.exec("""\
def double(x):
    return x * 2

result = double(21)
""")
    assert result.error is None
    assert result.namespace["result"] == 42


def test_class_def(sandbox):
    result = sandbox.exec("""\
class Counter:
    def __init__(self):
        self.n = 0
    def inc(self):
        self.n += 1

c = Counter()
c.inc()
c.inc()
result = c.n
""")
    assert result.error is None
    assert result.namespace["result"] == 2


def test_comprehensions(sandbox):
    result = sandbox.exec("""\
squares = [x**2 for x in range(5)]
evens = {x for x in range(10) if x % 2 == 0}
""")
    assert result.error is None
    assert result.namespace["squares"] == [0, 1, 4, 9, 16]
    assert result.namespace["evens"] == {0, 2, 4, 6, 8}


def test_exception_handling(sandbox):
    result = sandbox.exec("""\
try:
    x = 1 / 0
except ZeroDivisionError:
    x = -1
""")
    assert result.error is None
    assert result.namespace["x"] == -1


def test_runtime_error_captured(sandbox):
    result = sandbox.exec("x = 1 / 0")
    assert isinstance(result.error, ZeroDivisionError)


def test_syntax_error_captured(sandbox):
    result = sandbox.exec("def")
    assert isinstance(result.error, SyntaxError)


def test_validation_error_on_result(sandbox):
    result = sandbox.exec("__st_foo = 1")
    assert isinstance(result.error, StValidationError)


def test_error_traceback_has_sandtrap_filename(sandbox):
    result = sandbox.exec("x = 1\ny = 1/0\nz = 3")
    assert result.error is not None
    import traceback

    tb_text = "".join(
        traceback.format_exception(
            type(result.error), result.error, result.error.__traceback__
        )
    )
    assert "<sandtrap:" in tb_text


def test_namespace_passthrough(sandbox):
    result = sandbox.exec("y = x + 1", namespace={"x": 10})
    assert result.error is None
    assert result.namespace["y"] == 11


def test_namespace_no_builtins_leak(sandbox):
    result = sandbox.exec("x = 1")
    assert "__builtins__" not in result.namespace


def test_fstring(sandbox):
    result = sandbox.exec("name = 'world'\nresult = f'hello {name}'")
    assert result.error is None
    assert result.namespace["result"] == "hello world"


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings require 3.14+")
def test_tstring():
    import string.templatelib

    policy = Policy(timeout=5.0)
    policy.module(string.templatelib)
    sb = Sandbox(policy)
    result = sb.exec("""\
from string.templatelib import Template
name = 'world'
result = t'hello {name}'
is_template = isinstance(result, Template)
""")
    assert result.error is None
    assert result.namespace["is_template"] is True


def test_walrus_operator(sandbox):
    result = sandbox.exec("""\
results = []
for x in range(5):
    if (y := x * 2) > 4:
        results.append(y)
""")
    assert result.error is None
    assert result.namespace["results"] == [6, 8]


def test_lambda(sandbox):
    result = sandbox.exec("""\
double = lambda x: x * 2
result = double(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 10


def test_generators(sandbox):
    result = sandbox.exec("""\
def gen():
    yield 1
    yield 2
    yield 3

result = list(gen())
""")
    assert result.error is None
    assert result.namespace["result"] == [1, 2, 3]


def test_nested_functions(sandbox):
    result = sandbox.exec("""\
def make_adder(n):
    def add(x):
        return x + n
    return add

add5 = make_adder(5)
result = add5(3)
""")
    assert result.error is None
    assert result.namespace["result"] == 8


def test_dict_operations(sandbox):
    result = sandbox.exec("""\
d = {'a': 1, 'b': 2}
d['c'] = 3
result = {k: v for k, v in d.items() if v > 1}
""")
    assert result.error is None
    assert result.namespace["result"] == {"b": 2, "c": 3}


def test_safe_locals_returns_user_vars(sandbox):
    """locals() returns user-defined variables."""
    result = sandbox.exec("""\
x = 1
y = 'hello'
loc = locals()
""")
    assert result.error is None
    loc = result.namespace["loc"]
    assert loc["x"] == 1
    assert loc["y"] == "hello"


def test_safe_locals_excludes_internals(sandbox):
    """locals() does not expose sandbox internals."""
    result = sandbox.exec("""\
loc = locals()
keys = list(loc.keys())
""")
    assert result.error is None
    keys = result.namespace["keys"]
    for k in keys:
        assert not k.startswith("__st_"), f"Internal key leaked: {k}"
        assert k != "__builtins__"
        assert k != "__name__"
        assert k != "print"


def test_safe_locals_is_a_copy(sandbox):
    """Mutating the dict returned by locals() doesn't affect the namespace."""
    result = sandbox.exec("""\
x = 1
loc = locals()
loc['x'] = 999
result = x
""")
    assert result.error is None
    assert result.namespace["result"] == 1


def test_safe_locals_cannot_replace_gates(sandbox):
    """Cannot overwrite gate functions via locals()."""
    result = sandbox.exec("""\
loc = locals()
# Even if someone tries to write a gate name into the copy, it doesn't matter
loc['__st_getattr__'] = lambda obj, attr: getattr(obj, attr)
# The real gate is still in place — this should still be blocked
class Foo:
    _secret = 42
f = Foo()
try:
    val = f._secret
    result = 'escaped'
except AttributeError:
    result = 'blocked'
""")
    assert result.error is None
    assert result.namespace["result"] == "blocked"


def test_safe_locals_inside_function(sandbox):
    """locals() inside a function returns that function's locals."""
    result = sandbox.exec("""\
def check():
    a = 10
    b = 20
    return locals()

result = check()
""")
    assert result.error is None
    assert result.namespace["result"] == {"a": 10, "b": 20}


def test_globals_not_available(sandbox):
    """globals() is not available in sandboxed code."""
    result = sandbox.exec("g = globals()")
    assert result.error is not None
    assert isinstance(result.error, NameError)


def test_print_file_kwarg_rejected(sandbox):
    """print(file=...) raises an error."""
    result = sandbox.exec("print('hello', file='something')")
    assert result.error is not None
    assert isinstance(result.error, ValueError)
    assert "file=" in str(result.error)


def test_base_exception_captured(sandbox):
    """BaseException raised in sandbox is captured, not propagated."""
    result = sandbox.exec("raise Exception('test')")
    assert result.error is not None
    assert isinstance(result.error, Exception)


def test_generator_exit_not_available(sandbox):
    """GeneratorExit is not available as a name in sandbox."""
    result = sandbox.exec("x = GeneratorExit")
    assert result.error is not None
    assert isinstance(result.error, NameError)


def test_keyboard_interrupt_not_available(sandbox):
    """KeyboardInterrupt is not available as a name in sandbox."""
    result = sandbox.exec("x = KeyboardInterrupt")
    assert result.error is not None
    assert isinstance(result.error, NameError)


def test_base_exception_not_available(sandbox):
    """BaseException is not available as a name in sandbox."""
    result = sandbox.exec("x = BaseException")
    assert result.error is not None
    assert isinstance(result.error, NameError)


def test_constructable_false_blocks_construction():
    """Classes with constructable=False cannot be instantiated."""

    class Secret:
        pass

    policy = Policy()
    policy.cls(Secret, constructable=False)
    sandbox = Sandbox(policy)
    result = sandbox.exec("s = Secret()")
    assert result.error is not None
    assert isinstance(result.error, TypeError)
    assert "not constructable" in str(result.error)


def test_constructable_false_isinstance_works():
    """Classes with constructable=False still work for isinstance checks."""

    class Marker:
        pass

    policy = Policy()
    policy.cls(Marker, constructable=False)
    sandbox = Sandbox(policy)
    # Pass an instance via namespace so isinstance can be checked
    result = sandbox.exec(
        "result = isinstance(obj, Marker)",
        namespace={"obj": Marker()},
    )
    assert result.error is None
    assert result.namespace["result"] is True


def test_result_namespace_excludes_print(sandbox):
    """print is not leaked into result.namespace."""
    result = sandbox.exec("x = 1")
    assert result.error is None
    assert "print" not in result.namespace


def test_result_namespace_excludes_registered_fn():
    """Registered functions are not leaked into result.namespace."""

    def helper():
        return 42

    policy = Policy()
    policy.fn(helper)
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = helper()")
    assert result.error is None
    assert result.namespace["x"] == 42
    assert "helper" not in result.namespace


def test_result_namespace_keeps_reassigned_print(sandbox):
    """If user reassigns print, it stays in result.namespace."""
    result = sandbox.exec("print = 42")
    assert result.error is None
    assert result.namespace["print"] == 42


def test_linecache_cleanup(sandbox):
    """linecache entries are cleaned up after execution."""
    import linecache

    before = len(linecache.cache)
    sandbox.exec("x = 1")
    after = len(linecache.cache)
    # Should not grow (entry added then removed)
    assert after == before


def test_type_single_arg_allowed(sandbox):
    """type(obj) inspection form works."""
    result = sandbox.exec("result = type(42)")
    assert result.error is None
    assert result.namespace["result"] is int


def test_type_three_arg_blocked(sandbox):
    """type('X', bases, dict) class-creation form is blocked."""
    result = sandbox.exec("X = type('X', (object,), {'a': 1})")
    assert result.error is not None
    assert isinstance(result.error, TypeError)


def test_st_name_read_blocked(sandbox):
    """Sandboxed code cannot read __st_* names."""
    result = sandbox.exec("x = __st_getattr__")
    assert isinstance(result.error, StValidationError)
    assert "Cannot reference reserved name" in str(result.error)


def test_aexec_st_locals_blocked():
    """Sandboxed code in aexec cannot call __st_locals__."""
    import asyncio

    sandbox = Sandbox(Policy())
    result = asyncio.run(sandbox.aexec("x = __st_locals__"))
    assert isinstance(result.error, StValidationError)
    assert "Cannot reference reserved name" in str(result.error)


def test_annassign_attr_goes_through_gate():
    """Annotated assignment to attribute goes through __st_setattr__ gate."""
    sandbox = Sandbox(Policy())
    result = sandbox.exec("""\
class Foo:
    pass
f = Foo()
f._secret: int = 42
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_annassign_attr_allowed():
    """Annotated assignment to a public attribute works."""
    sandbox = Sandbox(Policy())
    result = sandbox.exec("""\
class Foo:
    pass
f = Foo()
f.x: int = 42
result = f.x
""")
    assert result.error is None
    assert result.namespace["result"] == 42


def test_comprehension_respects_timeout():
    """Comprehensions respect the timeout via checkpoint."""
    from sandtrap.errors import StTimeout

    policy = Policy()
    policy.timeout = 0.1
    sandbox = Sandbox(policy)
    result = sandbox.exec("[i for i in range(10_000_000_000)]")
    assert isinstance(result.error, StTimeout)


def test_fstring_attribute_gated():
    """F-string attribute access goes through __st_getattr__ gate."""
    policy = Policy()
    sandbox = Sandbox(policy)

    # Public attribute should work
    result = sandbox.exec("""\
class Obj:
    name = "hello"
o = Obj()
result = f"value={o.name}"
""")
    assert result.error is None
    assert result.namespace["result"] == "value=hello"

    # Private attribute should be blocked
    result = sandbox.exec("""\
class Obj:
    _secret = 42
o = Obj()
result = f"value={o._secret}"
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_fstring_dunder_attribute_blocked():
    """F-string access to __class__ is blocked by policy."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
result = f"{(1).__class__}"
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_str_format_traversal_blocked():
    """str.format field traversal (e.g. {0.__class__}) is blocked."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
result = "{0.__class__}".format(42)
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_attributeerror_obj_no_leak_on_policy_block():
    """Policy-blocked attr raises AttributeError with .obj = None (CVE-2026-0863 pattern)."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
class Foo:
    _secret = 42
f = Foo()
try:
    f._secret
except AttributeError as e:
    obj_val = getattr(e, 'obj', 'NOT SET')
""")
    assert result.error is None
    assert result.namespace["obj_val"] is None


def test_attributeerror_obj_no_escalation():
    """Even with .obj set, dunder traversal is blocked (CVE-2026-0863 pattern)."""
    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
class Foo:
    pass
f = Foo()
try:
    f.nonexistent
except AttributeError as e:
    obj = getattr(e, 'obj', None)
    # Attempt n8n-style escalation: obj -> type -> __subclasses__
    t = type(obj)
    try:
        t.__subclasses__
        escaped = True
    except AttributeError:
        escaped = False
""")
    assert result.error is None
    assert result.namespace["escaped"] is False


def test_builtins_not_accessible():
    """Sandboxed code cannot access __builtins__ at all (AST blocked)."""
    from sandtrap.errors import StValidationError

    policy = Policy()
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = __builtins__")
    assert result.error is not None
    assert isinstance(result.error, StValidationError)
    assert "__builtins__" in str(result.error)


def test_negative_timeout_still_runs():
    """Negative timeout doesn't prevent execution."""
    policy = Policy()
    policy.timeout = -1
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = 1 + 1")
    # Should either error or succeed — not hang
    # (negative timeout means already expired)
    assert result.namespace.get("x") == 2 or result.error is not None


def test_negative_memory_limit_still_runs():
    """Negative memory limit doesn't prevent execution."""
    policy = Policy()
    policy.memory_limit = -1
    sandbox = Sandbox(policy)
    result = sandbox.exec("x = 1 + 1")
    assert result.namespace.get("x") == 2 or result.error is not None


def test_sandbox_context_manager(sandbox):
    """Sandbox works as a context manager."""
    with sandbox as sbx:
        result = sbx.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


def test_fs_patches_installed_permanently():
    """FS patches are installed on first exec and remain active."""
    import importlib

    from sandtrap import VirtualFS

    _install_mod = importlib.import_module("monkeyfs.patching.install")

    fs = VirtualFS({})
    sandbox = Sandbox(Policy(), filesystem=fs)
    sandbox.exec("x = 1")
    assert _install_mod._installed


def test_net_patches_installed_permanently():
    """Net patches are installed once and remain active."""
    from sandtrap.net import patch as net_patch

    with Sandbox(Policy(allow_network=False)):
        assert net_patch._installed
    # Patches remain after exit
    assert net_patch._installed


def test_overlapping_sandboxes():
    """Overlapping sandboxes each see their own filesystem."""
    from sandtrap import VirtualFS

    fs1 = VirtualFS({})
    fs1.write("/a.txt", b"from fs1")
    fs2 = VirtualFS({})
    fs2.write("/a.txt", b"from fs2")

    sandbox1 = Sandbox(Policy(), filesystem=fs1)
    sandbox2 = Sandbox(Policy(), filesystem=fs2)

    r1 = sandbox1.exec("f = open('/a.txt'); content = f.read(); f.close()")
    assert r1.error is None
    assert r1.namespace["content"] == "from fs1"

    r2 = sandbox2.exec("f = open('/a.txt'); content = f.read(); f.close()")
    assert r2.error is None
    assert r2.namespace["content"] == "from fs2"
