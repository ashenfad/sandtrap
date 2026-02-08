"""Tests for find_refs static analysis."""

from sblite import find_refs


def test_simple_load():
    refs = find_refs("x + y")
    assert refs == {"x", "y"}


def test_assignment_not_a_ref():
    refs = find_refs("x = 1")
    assert refs == set()


def test_load_then_assign():
    """If a name is loaded before being assigned, it's a ref."""
    refs = find_refs("y = x + 1\nx = 5")
    assert "x" in refs
    # y is assigned first, then used — but the assignment binds it
    assert "y" not in refs


def test_assign_then_load():
    """Even if assigned before loaded, conservatively reported as a ref."""
    refs = find_refs("x = 1\ny = x + 1")
    # Conservative: x is loaded anywhere, so it's reported
    assert "x" in refs
    assert "y" not in refs


def test_function_def_binds_name():
    refs = find_refs("def f(x): return x + 1")
    assert "f" not in refs
    assert "x" not in refs


def test_function_free_vars():
    """Free variables inside a function are module-level refs."""
    refs = find_refs("""\
def compute(x):
    return x + offset
""")
    assert "offset" in refs
    assert "x" not in refs
    assert "compute" not in refs


def test_function_with_default():
    """Default values are evaluated in the enclosing scope."""
    refs = find_refs("def f(x=default_val): return x")
    assert "default_val" in refs
    assert "x" not in refs


def test_class_def_binds_name():
    refs = find_refs("class Foo: pass")
    assert "Foo" not in refs


def test_class_bases_are_refs():
    refs = find_refs("class Child(Base): pass")
    assert "Base" in refs
    assert "Child" not in refs


def test_class_decorators_are_refs():
    refs = find_refs("""\
@decorator
class Foo:
    pass
""")
    assert "decorator" in refs


def test_class_body_free_vars():
    """Names used in class body that aren't locally defined."""
    refs = find_refs("""\
class Foo:
    x = external_value
""")
    assert "external_value" in refs


def test_import_binds_name():
    refs = find_refs("import math")
    assert "math" not in refs


def test_import_as_binds_alias():
    refs = find_refs("import numpy as np")
    assert "np" not in refs
    assert "numpy" not in refs


def test_from_import_binds_name():
    refs = find_refs("from math import sqrt")
    assert "sqrt" not in refs
    assert "math" not in refs


def test_for_loop_binds_target():
    refs = find_refs("for i in items: pass")
    assert "i" not in refs
    assert "items" in refs


def test_with_binds_target():
    refs = find_refs("with ctx() as f: pass")
    assert "f" not in refs
    assert "ctx" in refs


def test_except_binds_name():
    refs = find_refs("""\
try:
    risky()
except ValueError as e:
    handle(e)
""")
    assert "risky" in refs
    assert "handle" in refs
    assert "ValueError" in refs
    # e is bound by except but also loaded later — conservative reports it
    assert "e" in refs


def test_nested_function_free_vars():
    """Free vars in nested functions bubble up."""
    refs = find_refs("""\
def outer():
    def inner():
        return global_var
""")
    assert "global_var" in refs


def test_closure_does_not_bubble():
    """A var bound in outer that inner uses is not a module-level ref."""
    refs = find_refs("""\
def outer():
    x = 1
    def inner():
        return x
""")
    assert "x" not in refs


def test_lambda_free_vars():
    refs = find_refs("f = lambda x: x + offset")
    assert "offset" in refs
    assert "x" not in refs
    assert "f" not in refs


def test_walrus_operator():
    refs = find_refs("if (n := compute()): print(n)")
    assert "compute" in refs
    assert "print" in refs
    # n is bound by walrus but also loaded later — conservative reports it
    assert "n" in refs


def test_builtins_excluded():
    """True, False, None are not reported as refs."""
    refs = find_refs("x = True\ny = None")
    assert "True" not in refs
    assert "None" not in refs


def test_sb_names_excluded():
    """Internal __sb_* names are not reported."""
    refs = find_refs("__sb_getattr__(x, 'y')")
    assert "__sb_getattr__" not in refs
    assert "x" in refs


def test_augmented_assign():
    """x += 1 both reads and writes x."""
    refs = find_refs("x += 1")
    assert "x" in refs


def test_method_call():
    refs = find_refs("result = obj.method(arg)")
    assert "obj" in refs
    assert "arg" in refs
    assert "result" not in refs


def test_complex_expression():
    refs = find_refs("result = [f(x) for x in data if pred(x)]")
    assert "f" in refs
    assert "data" in refs
    assert "pred" in refs
    assert "result" not in refs


def test_function_decorator_is_ref():
    refs = find_refs("""\
@my_decorator
def f():
    pass
""")
    assert "my_decorator" in refs
    assert "f" not in refs


def test_global_in_function():
    """global declaration in a function — the var comes from module scope."""
    refs = find_refs("""\
def f():
    global x
    x = 1
""")
    # x is declared global but only written, not read — not a ref
    assert "x" not in refs


def test_global_read_in_function():
    refs = find_refs("""\
def f():
    global x
    return x
""")
    # x is declared global and read — but it's a global, not free
    assert "x" not in refs


def test_nonlocal_bubbles_up():
    """nonlocal in inner function reads from outer, which may read from module."""
    refs = find_refs("""\
def outer():
    def inner():
        nonlocal x
        return x
""")
    # x is nonlocal in inner, which means it's free in inner,
    # and since x is not bound in outer either, it bubbles up
    assert "x" in refs


def test_empty_source():
    refs = find_refs("")
    assert refs == set()


def test_syntax_preserves_all_refs():
    """Multiple statement types in a realistic snippet."""
    refs = find_refs("""\
import math
from collections import Counter

data = prepare(raw_input)
counts = Counter(data)
result = math.sqrt(counts['a'] + offset)
print(result)
""")
    assert "prepare" in refs
    assert "raw_input" in refs
    assert "offset" in refs
    assert "print" in refs
    # Locally defined names that are also loaded are conservatively included
    assert "math" in refs
    assert "Counter" in refs
    assert "data" in refs
    assert "counts" in refs
    assert "result" in refs


def test_tuple_unpack_binds():
    refs = find_refs("a, b = get_pair()")
    assert "get_pair" in refs
    assert "a" not in refs
    assert "b" not in refs


def test_starred_unpack_binds():
    refs = find_refs("first, *rest = items")
    assert "items" in refs
    assert "first" not in refs
    assert "rest" not in refs


# --- Transitive dependency tests ---


def test_find_refs_with_namespace_follows_deps():
    """find_refs with namespace follows SbFunction.global_refs."""
    from sblite import Policy, Sandbox

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def square(x): return x * x
def sum_squares(lst):
    return sum(square(x) for x in lst)
""")
    assert result.error is None
    ns = result.namespace

    refs = find_refs("result = sum_squares([1, 2, 3])", namespace=ns)
    assert "sum_squares" in refs
    assert "square" in refs  # Discovered transitively


def test_find_refs_transitive_chain():
    """A -> B -> C chain is fully discovered."""
    from sblite import Policy, Sandbox

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def c(x): return x + 1
def b(x): return c(x) * 2
def a(x): return b(x) + 10
""")
    assert result.error is None
    ns = result.namespace

    refs = find_refs("result = a(5)", namespace=ns)
    assert "a" in refs
    assert "b" in refs
    assert "c" in refs


def test_find_refs_cycle_detection():
    """Circular deps (A -> B -> A) don't cause infinite loop."""
    from sblite import Policy, Sandbox

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Both reference each other as globals (even if it would recurse at runtime)
    result = sandbox.exec("""\
def a(x):
    if x <= 0:
        return 0
    return b(x - 1)
def b(x):
    if x <= 0:
        return 0
    return a(x - 1)
""")
    assert result.error is None
    ns = result.namespace

    refs = find_refs("result = a(5)", namespace=ns)
    assert "a" in refs
    assert "b" in refs


def test_find_refs_no_namespace_unchanged():
    """Without namespace, find_refs behaves exactly as before."""
    refs_without = find_refs("result = f(x)")
    refs_with_none = find_refs("result = f(x)", namespace=None)
    assert refs_without == refs_with_none
    assert refs_without == {"f", "x"}


def test_find_refs_namespace_non_sbfunction_ignored():
    """Plain values in namespace are not followed."""
    refs = find_refs("result = f(x)", namespace={"f": lambda x: x, "x": 42})
    assert refs == {"f", "x"}
