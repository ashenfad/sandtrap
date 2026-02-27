"""Stress tests: escape attempts that should be blocked."""

import pytest

from sandtrap import Policy, Sandbox
from sandtrap.errors import StValidationError


@pytest.fixture
def sandbox():
    return Sandbox(Policy(timeout=5.0))


# --- MRO / dunder traversal ---


def test_class_base_traversal(sandbox):
    """().__class__.__bases__[0] should be blocked by attr gate."""
    result = sandbox.exec("x = ().__class__")
    assert result.error is not None  # __class__ is private


def test_mro_traversal(sandbox):
    result = sandbox.exec("x = ().__class__.__mro__")
    assert result.error is not None


def test_subclasses_traversal(sandbox):
    result = sandbox.exec("x = type.__subclasses__(type)")
    assert result.error is not None


def test_object_subclasses(sandbox):
    result = sandbox.exec("x = object.__subclasses__()")
    assert result.error is not None


def test_globals_via_function(sandbox):
    """func.__globals__ should be blocked."""
    result = sandbox.exec("""\
def f(): pass
g = f.__globals__
""")
    assert result.error is not None


def test_code_object_access(sandbox):
    """func.__code__ should be blocked."""
    result = sandbox.exec("""\
def f(): pass
c = f.__code__
""")
    assert result.error is not None


# --- Builtins probing ---


def test_no_eval(sandbox):
    result = sandbox.exec("eval('1+1')")
    assert result.error is not None


def test_no_exec(sandbox):
    result = sandbox.exec("exec('x = 1')")
    assert result.error is not None


def test_no_compile(sandbox):
    result = sandbox.exec("compile('x=1', '<>', 'exec')")
    assert result.error is not None


def test_no_dunder_import(sandbox):
    result = sandbox.exec("__import__('os')")
    assert result.error is not None


def test_no_globals_builtin(sandbox):
    result = sandbox.exec("g = globals()")
    assert result.error is not None


def test_no_vars_builtin(sandbox):
    result = sandbox.exec("v = vars()")
    assert result.error is not None


def test_dir_builtin_allowed(sandbox):
    result = sandbox.exec("x = 1\nd = dir()")
    assert result.error is None
    assert isinstance(result.namespace["d"], list)
    assert "x" in result.namespace["d"]
    # Sandbox internals should be filtered out
    assert not any(name.startswith("__st_") for name in result.namespace["d"])
    assert "__builtins__" not in result.namespace["d"]


def test_dir_with_argument(sandbox):
    result = sandbox.exec("d = dir([1, 2, 3])")
    assert result.error is None
    assert "append" in result.namespace["d"]


def test_help_with_argument(sandbox):
    result = sandbox.exec("help(int)")
    assert result.error is None
    assert "int" in result.stdout


def test_help_output_in_prints():
    """help() output lands in result.prints as plain strings when snapshot_prints is on."""
    sb = Sandbox(Policy(), snapshot_prints=True)
    result = sb.exec("help(len)")
    assert result.error is None
    combined = "".join(t[0] for t in result.prints)
    assert "len" in combined


def test_help_string_import_blocked(sandbox):
    """help('os') would trigger pydoc module import — must be blocked."""
    result = sandbox.exec("help('os')")
    assert result.error is not None
    assert isinstance(result.error, TypeError)


def test_no_breakpoint(sandbox):
    result = sandbox.exec("breakpoint()")
    assert result.error is not None


def test_no_open_without_filesystem(sandbox):
    result = sandbox.exec("f = open('/etc/passwd')")
    assert result.error is not None


def test_no_input(sandbox):
    result = sandbox.exec("x = input()")
    assert result.error is not None


# --- Builtins mutation ---


def test_builtins_not_readable(sandbox):
    """__builtins__ cannot be accessed from sandboxed code."""
    from sandtrap.errors import StValidationError

    result = sandbox.exec("x = __builtins__")
    assert result.error is not None
    assert isinstance(result.error, StValidationError)


# --- Gate evasion ---


def test_st_name_read_blocked(sandbox):
    """Reading __st_* names is blocked at validation time."""
    result = sandbox.exec("x = __st_getattr__")
    assert isinstance(result.error, StValidationError)


def test_st_name_assign_blocked(sandbox):
    result = sandbox.exec("__st_getattr__ = lambda o, a: getattr(o, a)")
    assert isinstance(result.error, StValidationError)


def test_st_name_delete_blocked(sandbox):
    result = sandbox.exec("del __st_checkpoint__")
    assert isinstance(result.error, StValidationError)


def test_st_global_blocked(sandbox):
    result = sandbox.exec("""\
def f():
    global __st_getattr__
""")
    assert isinstance(result.error, StValidationError)


def test_st_nonlocal_blocked(sandbox):
    result = sandbox.exec("""\
def f():
    __st_x = 1
    def g():
        nonlocal __st_x
""")
    assert isinstance(result.error, StValidationError)


def test_assign_to_exec_blocked(sandbox):
    result = sandbox.exec("exec = print")
    assert isinstance(result.error, StValidationError)


def test_assign_to_eval_blocked(sandbox):
    result = sandbox.exec("eval = print")
    assert isinstance(result.error, StValidationError)


def test_assign_to_compile_blocked(sandbox):
    result = sandbox.exec("compile = print")
    assert isinstance(result.error, StValidationError)


def test_delete_exec_blocked(sandbox):
    result = sandbox.exec("del exec")
    assert isinstance(result.error, StValidationError)


# --- Import shenanigans ---


def test_import_os(sandbox):
    result = sandbox.exec("import os")
    assert isinstance(result.error, ImportError)


def test_import_sys(sandbox):
    result = sandbox.exec("import sys")
    assert isinstance(result.error, ImportError)


def test_import_subprocess(sandbox):
    result = sandbox.exec("import subprocess")
    assert isinstance(result.error, ImportError)


def test_import_ctypes(sandbox):
    result = sandbox.exec("import ctypes")
    assert isinstance(result.error, ImportError)


def test_from_builtins_import(sandbox):
    result = sandbox.exec("from builtins import open")
    assert isinstance(result.error, ImportError)


def test_wildcard_import_blocked(sandbox):
    result = sandbox.exec("from os import *")
    assert isinstance(result.error, StValidationError)


# --- Format string traversal ---


def test_format_string_attr_access(sandbox):
    """'{0.__class__}'.format(x) should be blocked."""
    result = sandbox.exec("x = '{0.__class__}'.format(42)")
    assert result.error is not None


def test_format_string_item_access(sandbox):
    result = sandbox.exec("x = '{0[__class__]}'.format({'__class__': 'bad'})")
    assert result.error is not None


def test_fstring_attr_access(sandbox):
    """f-strings with attribute access go through the getattr gate."""
    result = sandbox.exec("x = 42; y = f'{x.__class__}'")
    assert result.error is not None


def test_fstring_format_spec(sandbox):
    """f-string with format spec still gates attribute access."""
    result = sandbox.exec("x = 42; y = f'{x.__class__!r:>20}'")
    assert result.error is not None


def test_fstring_nested_format_spec(sandbox):
    """f-string with nested format spec doesn't bypass gates."""
    result = sandbox.exec("""\
class Obj:
    _secret = 42
o = Obj()
w = 10
y = f'{o._secret:{w}}'
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_fstring_conversion_safe(sandbox):
    """f-string !r/!s/!a conversions on safe values work fine."""
    result = sandbox.exec("""\
x = 42
a = f'{x!r}'
b = f'{x!s}'
c = f'{x!a}'
""")
    assert result.error is None
    assert result.namespace["a"] == "42"


def test_fstring_method_call_gated(sandbox):
    """f-string calling a gated method is blocked."""
    result = sandbox.exec("""\
class Obj:
    _hidden = 99
o = Obj()
y = f'{o._hidden.__str__()}'
""")
    assert result.error is not None


# --- type() three-arg form ---


def test_type_three_arg_blocked(sandbox):
    """type('X', (object,), {}) should be blocked."""
    result = sandbox.exec("X = type('X', (object,), {})")
    assert result.error is not None


def test_type_one_arg_allowed(sandbox):
    """type(42) should work."""
    result = sandbox.exec("t = type(42)")
    assert result.error is None
    assert result.namespace["t"] is int


def test_type_via_alias_blocked(sandbox):
    """Assigning type to a variable and calling 3-arg form is blocked."""
    result = sandbox.exec("t = type; X = t('Foo', (object,), {})")
    assert result.error is not None


def test_type_via_class_class(sandbox):
    """obj.__class__.__class__ is blocked by attribute gate."""
    result = sandbox.exec("x = (1).__class__.__class__")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_type_via_mro_to_type(sandbox):
    """Traversing MRO to reach type is blocked."""
    result = sandbox.exec("""\
class Foo:
    pass
t = Foo.__mro__[-1].__class__
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


# --- Frame / generator internal access ---


def test_generator_frame_access_blocked(sandbox):
    """gi_frame should be blocked to prevent namespace tampering."""
    result = sandbox.exec("""\
def gen():
    yield 1
g = gen()
f = g.gi_frame
""")
    assert result.error is not None


def test_generator_code_access_blocked(sandbox):
    result = sandbox.exec("""\
def gen():
    yield 1
g = gen()
c = g.gi_code
""")
    assert result.error is not None


def test_coroutine_frame_access_blocked(sandbox):
    result = sandbox.exec("""\
async def coro():
    return 1
c = coro()
try:
    f = c.cr_frame
finally:
    c.close()
""")
    assert result.error is not None


def test_checkpoint_bypass_via_frame(sandbox):
    """The full attack: reach f_globals and replace checkpoint gate."""
    result = sandbox.exec("""\
def gen():
    yield 1
g = gen()
ns = g.gi_frame.f_globals
""")
    assert result.error is not None
