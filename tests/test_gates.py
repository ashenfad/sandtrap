"""Tests for attribute access gates (Phase 2)."""

import pytest

from sblite import Policy, Sandbox


@pytest.fixture
def sandbox():
    return Sandbox(Policy())


def test_attr_read(sandbox):
    """obj.attr in Load context goes through __sb_getattr__."""
    result = sandbox.exec("""\
class Obj:
    x = 42
o = Obj()
result = o.x
""")
    assert result.error is None
    assert result.namespace["result"] == 42


def test_attr_write(sandbox):
    """obj.attr = value goes through __sb_setattr__."""
    result = sandbox.exec("""\
class Obj:
    pass
o = Obj()
o.x = 99
result = o.x
""")
    assert result.error is None
    assert result.namespace["result"] == 99


def test_attr_delete(sandbox):
    """del obj.attr goes through __sb_delattr__."""
    result = sandbox.exec("""\
class Obj:
    pass
o = Obj()
o.x = 1
del o.x
result = hasattr(o, 'x')
""")
    assert result.error is None
    assert result.namespace["result"] is False


def test_chained_attr_read(sandbox):
    """a.b.c chains through multiple __sb_getattr__ calls."""
    result = sandbox.exec("""\
class Inner:
    val = 10
class Outer:
    pass
o = Outer()
o.inner = Inner()
result = o.inner.val
""")
    assert result.error is None
    assert result.namespace["result"] == 10


def test_method_call(sandbox):
    """obj.method() works — getattr returns the bound method, then called."""
    result = sandbox.exec("""\
class Calc:
    def add(self, a, b):
        return a + b
c = Calc()
result = c.add(3, 4)
""")
    assert result.error is None
    assert result.namespace["result"] == 7


def test_augmented_assign_attr(sandbox):
    """obj.x += 1 decomposes into get + op + set."""
    result = sandbox.exec("""\
class Counter:
    def __init__(self):
        self.n = 0
c = Counter()
c.n += 5
c.n += 3
result = c.n
""")
    assert result.error is None
    assert result.namespace["result"] == 8


def test_augmented_assign_evaluates_obj_once(sandbox):
    """obj.x += 1 evaluates obj only once (important for side effects)."""
    result = sandbox.exec("""\
call_count = 0
class Box:
    val = 0

box = Box()

def get_box():
    global call_count
    call_count += 1
    return box

get_box().val += 10
result_val = box.val
result_calls = call_count
""")
    assert result.error is None
    assert result.namespace["result_val"] == 10
    assert result.namespace["result_calls"] == 1


def test_blocked_private_attr(sandbox):
    """Accessing _private attrs is blocked by default policy."""
    result = sandbox.exec("""\
class Obj:
    _secret = 42
o = Obj()
x = o._secret
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_blocked_unsafe_dunder(sandbox):
    """Accessing __code__ is blocked by default policy."""
    result = sandbox.exec("""\
def f(): pass
x = f.__code__
""")
    assert result.error is not None
    assert isinstance(result.error, AttributeError)


def test_allowed_dunders(sandbox):
    """Allowed dunders like __len__, __str__ work."""
    result = sandbox.exec("""\
class MyList:
    def __init__(self):
        self.items = [1, 2, 3]
    def __len__(self):
        return len(self.items)
    def __str__(self):
        return str(self.items)

ml = MyList()
length = ml.__len__()
s = ml.__str__()
""")
    assert result.error is None
    assert result.namespace["length"] == 3
    assert result.namespace["s"] == "[1, 2, 3]"


def test_fstring_with_attr(sandbox):
    """f-string accessing obj.attr goes through gate."""
    result = sandbox.exec("""\
class Person:
    name = 'Alice'
p = Person()
result = f'Hello, {p.name}!'
""")
    assert result.error is None
    assert result.namespace["result"] == "Hello, Alice!"


def test_multi_target_assign_with_attr(sandbox):
    """x = obj.attr = value stores value in both."""
    result = sandbox.exec("""\
class Obj:
    pass
o = Obj()
x = o.val = 42
result_x = x
result_o = o.val
""")
    assert result.error is None
    assert result.namespace["result_x"] == 42
    assert result.namespace["result_o"] == 42


def test_tuple_unpack_with_attr(sandbox):
    """a, obj.x = (1, 2) decomposes correctly."""
    result = sandbox.exec("""\
class Obj:
    pass
o = Obj()
a, o.x = 10, 20
result_a = a
result_x = o.x
""")
    assert result.error is None
    assert result.namespace["result_a"] == 10
    assert result.namespace["result_x"] == 20


def test_class_with_self_attrs(sandbox):
    """Full class with self.attr patterns works end to end."""
    result = sandbox.exec("""\
class Stack:
    def __init__(self):
        self.items = []
    def push(self, item):
        self.items.append(item)
    def pop(self):
        return self.items.pop()
    def size(self):
        return len(self.items)

s = Stack()
s.push(1)
s.push(2)
s.push(3)
popped = s.pop()
size = s.size()
""")
    assert result.error is None
    assert result.namespace["popped"] == 3
    assert result.namespace["size"] == 2


def test_gate_names_not_in_namespace(sandbox):
    """Gate function names (__sb_*) are cleaned from result namespace."""
    result = sandbox.exec("x = 1")
    assert result.error is None
    for key in result.namespace:
        assert not key.startswith("__sb_"), f"Gate name leaked: {key}"


def test_attr_on_builtin_types(sandbox):
    """Attribute access on builtin types works (e.g., str.upper)."""
    result = sandbox.exec("""\
s = 'hello'
result = s.upper()
""")
    assert result.error is None
    assert result.namespace["result"] == "HELLO"


def test_list_method_calls(sandbox):
    """List methods work through attribute gate."""
    result = sandbox.exec("""\
items = [3, 1, 2]
items.sort()
items.append(4)
result = items
""")
    assert result.error is None
    assert result.namespace["result"] == [1, 2, 3, 4]


def test_dict_method_calls(sandbox):
    """Dict methods work through attribute gate."""
    result = sandbox.exec("""\
d = {'a': 1, 'b': 2}
keys = list(d.keys())
vals = list(d.values())
""")
    assert result.error is None
    assert sorted(result.namespace["keys"]) == ["a", "b"]
    assert sorted(result.namespace["vals"]) == [1, 2]


def test_format_string_attr_blocked(sandbox):
    """str.format() with attribute traversal is blocked."""
    result = sandbox.exec("x = '{0.__class__}'.format(42)")
    assert result.error is not None
    assert "format string" in str(result.error).lower()


def test_format_string_item_blocked(sandbox):
    """str.format() with item traversal is blocked."""
    result = sandbox.exec("x = '{0[secret]}'.format({'secret': 'leaked'})")
    assert result.error is not None
    assert "format string" in str(result.error).lower()


def test_format_map_attr_blocked(sandbox):
    """str.format_map() with attribute traversal is blocked."""
    result = sandbox.exec("x = '{obj.__class__}'.format_map({'obj': 42})")
    assert result.error is not None
    assert "format string" in str(result.error).lower()


def test_format_string_simple_fields_allowed(sandbox):
    """str.format() with simple positional/keyword fields works."""
    result = sandbox.exec("""\
a = '{0} + {1} = {2}'.format(1, 2, 3)
b = '{name} is {age}'.format(name='Alice', age=30)
""")
    assert result.error is None
    assert result.namespace["a"] == "1 + 2 = 3"
    assert result.namespace["b"] == "Alice is 30"


def test_getattr_enforces_policy(sandbox):
    """Builtin getattr() routes through the attr whitelist."""
    result = sandbox.exec("""\
class Obj:
    _secret = 42
    public = 99
o = Obj()
a = getattr(o, 'public')
b = getattr(o, '_secret', 'blocked')
""")
    assert result.error is None
    assert result.namespace["a"] == 99
    assert result.namespace["b"] == "blocked"


def test_hasattr_enforces_policy(sandbox):
    """Builtin hasattr() routes through the attr whitelist."""
    result = sandbox.exec("""\
class Obj:
    _secret = 42
    public = 99
o = Obj()
a = hasattr(o, 'public')
b = hasattr(o, '_secret')
""")
    assert result.error is None
    assert result.namespace["a"] is True
    assert result.namespace["b"] is False
