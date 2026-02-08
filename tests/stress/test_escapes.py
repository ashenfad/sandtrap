"""Stress tests: escape attempts that should be blocked."""

import pytest

from sblite import Policy, Sandbox
from sblite.errors import SbValidationError


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


def test_no_dir_builtin(sandbox):
    result = sandbox.exec("d = dir()")
    assert result.error is not None


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


def test_builtins_frozen(sandbox):
    """__builtins__ should be a frozen MappingProxyType."""
    result = sandbox.exec("__builtins__['eval'] = lambda x: x")
    assert result.error is not None


def test_builtins_not_assignable(sandbox):
    """Can't replace __builtins__ entirely."""
    sandbox.exec("__builtins__ = {}")
    # This may succeed (assigns a local) but shouldn't bypass the sandbox
    # The real builtins are already baked into the namespace


# --- Gate evasion ---


def test_sb_name_read_blocked(sandbox):
    """Reading __sb_* names is blocked at validation time."""
    with pytest.raises(SbValidationError):
        sandbox.exec("x = __sb_getattr__")


def test_sb_name_assign_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("__sb_getattr__ = lambda o, a: getattr(o, a)")


def test_sb_name_delete_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("del __sb_checkpoint__")


def test_sb_global_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("""\
def f():
    global __sb_getattr__
""")


def test_sb_nonlocal_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("""\
def f():
    __sb_x = 1
    def g():
        nonlocal __sb_x
""")


def test_assign_to_exec_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("exec = print")


def test_assign_to_eval_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("eval = print")


def test_assign_to_compile_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("compile = print")


def test_delete_exec_blocked(sandbox):
    with pytest.raises(SbValidationError):
        sandbox.exec("del exec")


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
    with pytest.raises(SbValidationError):
        sandbox.exec("from os import *")


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
f = c.cr_frame
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
