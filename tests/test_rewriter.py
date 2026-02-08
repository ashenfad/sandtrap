"""Tests for the AST rewriter validation."""

import ast

import pytest

from sblite.errors import SbValidationError
from sblite.rewriter import Rewriter


def _rewrite(source: str) -> ast.AST:
    tree = ast.parse(source)
    return Rewriter().visit(tree)


def test_simple_pass_through():
    tree = _rewrite("x = 1 + 2")
    assert isinstance(tree, ast.Module)


def test_block_sb_assignment():
    with pytest.raises(SbValidationError, match="Cannot assign to reserved name"):
        _rewrite("__sb_foo = 1")


def test_block_sb_tuple_unpack():
    with pytest.raises(SbValidationError, match="Cannot assign to reserved name"):
        _rewrite("a, __sb_x = 1, 2")


def test_block_sb_delete():
    with pytest.raises(SbValidationError, match="Cannot delete reserved name"):
        _rewrite("del __sb_foo")


def test_block_exec_assignment():
    with pytest.raises(SbValidationError, match="Cannot assign to 'exec'"):
        _rewrite("exec = 1")


def test_block_eval_assignment():
    with pytest.raises(SbValidationError, match="Cannot assign to 'eval'"):
        _rewrite("eval = lambda x: x")


def test_block_compile_assignment():
    with pytest.raises(SbValidationError, match="Cannot assign to 'compile'"):
        _rewrite("compile = None")


def test_block_import_assignment():
    with pytest.raises(SbValidationError, match="Cannot assign to '__import__'"):
        _rewrite("__import__ = None")


def test_block_sb_global():
    with pytest.raises(SbValidationError, match="Cannot declare.*global"):
        _rewrite("global __sb_foo")


def test_block_sb_nonlocal():
    with pytest.raises(SbValidationError, match="Cannot declare.*nonlocal"):
        _rewrite("""\
def f():
    nonlocal __sb_foo
""")


def test_block_wildcard_import():
    with pytest.raises(SbValidationError, match="Wildcard imports"):
        _rewrite("from os import *")


def test_function_def():
    _rewrite("def f(x, y=1): return x + y")


def test_class_def():
    _rewrite("class Foo:\n    pass")


def test_control_flow():
    _rewrite("""\
for i in range(10):
    if i > 5:
        break
    elif i == 3:
        continue
""")


def test_while_loop():
    _rewrite("while True:\n    pass")


def test_try_except():
    _rewrite("""\
try:
    x = 1
except ValueError as e:
    pass
finally:
    y = 2
""")


def test_comprehensions():
    _rewrite("[x for x in range(10)]")
    _rewrite("{x for x in range(10)}")
    _rewrite("{k: v for k, v in items}")
    _rewrite("(x for x in range(10))")


def test_match_case():
    _rewrite("""\
match x:
    case 1:
        y = 1
    case [a, b]:
        y = 2
    case _:
        y = 3
""")


def test_fstring():
    _rewrite("f'hello {name}'")


def test_lambda():
    _rewrite("f = lambda x: x + 1")


def test_walrus():
    _rewrite("if (n := 10) > 5: pass")


def test_async_def():
    _rewrite("async def f(): await g()")


def test_yield():
    _rewrite("def gen(): yield 1")


def test_augmented_assign():
    _rewrite("x = 0\nx += 1")


def test_annotated_assign():
    _rewrite("x: int = 1")


def test_allowed_name_read():
    """Reading exec/eval as names is fine; only Store context is blocked."""
    _rewrite("x = exec")
    _rewrite("y = eval")


def test_block_sb_name_load():
    """Reading __sb_* names is blocked."""
    with pytest.raises(SbValidationError, match="Cannot reference reserved name"):
        _rewrite("x = __sb_getattr__")


def test_block_sb_name_load_in_call():
    """Calling __sb_* names directly is blocked."""
    with pytest.raises(SbValidationError, match="Cannot reference reserved name"):
        _rewrite("__sb_checkpoint__()")


def test_block_del_exec():
    """Deleting blocked names like exec/eval is rejected."""
    with pytest.raises(SbValidationError, match="Cannot delete 'exec'"):
        _rewrite("del exec")


def test_block_del_eval():
    with pytest.raises(SbValidationError, match="Cannot delete 'eval'"):
        _rewrite("del eval")


def test_block_for_attr_target():
    """For loops with attribute targets are rejected."""
    with pytest.raises(SbValidationError, match="Attribute targets in for"):
        _rewrite("for obj.x in items: pass")


def test_block_for_tuple_attr_target():
    """For loops with attribute inside tuple target are rejected."""
    with pytest.raises(SbValidationError, match="Attribute targets in for"):
        _rewrite("for a, obj.x in items: pass")


def test_block_with_attr_target():
    """With statements with attribute targets are rejected."""
    with pytest.raises(SbValidationError, match="Attribute targets in with"):
        _rewrite("with cm() as obj.x: pass")


def test_relative_import_passes_level():
    """Relative imports are rewritten with _level keyword."""
    tree = _rewrite("from .foo import bar")
    assert isinstance(tree, ast.Module)


def test_relative_import_parent_passes_level():
    """Parent-level relative imports are rewritten with _level keyword."""
    tree = _rewrite("from ..foo import bar")
    assert isinstance(tree, ast.Module)


def test_block_del_method():
    """__del__ methods in classes are rejected."""
    with pytest.raises(SbValidationError, match="__del__ methods are not allowed"):
        _rewrite("""\
class Foo:
    def __del__(self):
        pass
""")


def test_block_async_del_method():
    """async __del__ methods are also rejected."""
    with pytest.raises(SbValidationError, match="__del__ methods are not allowed"):
        _rewrite("""\
class Foo:
    async def __del__(self):
        pass
""")
