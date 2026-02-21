"""Tests for StFunction and StClass wrappers."""

import pickle
import threading

import pytest

from sandtrap import Policy, Sandbox
from sandtrap.errors import StCancelled, StTickLimit, StTimeout
from sandtrap.wrappers import StClass, StFunction, StInstance


def test_wrapped_mode_creates_sbfunction():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("def f(x): return x + 1")
    assert result.error is None
    assert isinstance(result.namespace["f"], StFunction)


def test_raw_mode_creates_regular_function():
    policy = Policy()
    sandbox = Sandbox(policy, mode="raw")
    result = sandbox.exec("def f(x): return x + 1")
    assert result.error is None
    assert not isinstance(result.namespace["f"], StFunction)
    assert callable(result.namespace["f"])


def test_sbfunction_callable():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("def double(x): return x * 2")
    assert result.error is None
    f = result.namespace["double"]
    assert f(5) == 10


def test_sbfunction_metadata():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec('''\
def greet(name: str, greeting: str = "Hello") -> str:
    """Greet someone."""
    return f"{greeting}, {name}!"
''')
    assert result.error is None
    f = result.namespace["greet"]
    assert f.__name__ == "greet"
    assert f.__doc__ == "Greet someone."
    assert "name" in f.__annotations__
    assert f.__defaults__ == ("Hello",)


def test_sbfunction_pickle_roundtrip():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("def f(x): return x + 1")
    f = result.namespace["f"]

    # Pickle and unpickle
    data = pickle.dumps(f)
    f2 = pickle.loads(data)

    # Should be inactive after unpickle
    assert isinstance(f2, StFunction)
    with pytest.raises(RuntimeError, match="not active"):
        f2(1)

    # Activate and use
    sandbox.activate(f2)
    assert f2(2) == 3


def test_sbfunction_repr():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("def f(x): return x")
    f = result.namespace["f"]
    assert "active" in repr(f)

    data = pickle.dumps(f)
    f2 = pickle.loads(data)
    assert "inactive" in repr(f2)


def test_sbfunction_with_defaults():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("def add(a, b=10): return a + b")
    f = result.namespace["add"]
    assert f(5) == 15
    assert f(5, 20) == 25


def test_sbfunction_with_closure():
    """Functions that capture local scope variables."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
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
    # Inner function is also an StFunction
    assert isinstance(result.namespace["add5"], StFunction)


def test_sbfunction_closure_pickle_roundtrip():
    """Closure variables are frozen and restored on pickle round-trip."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
def make_adder(n):
    def add(x):
        return x + n
    return add

add5 = make_adder(5)
""")
    assert result.error is None
    add5 = result.namespace["add5"]
    assert add5(3) == 8

    # Pickle round-trip
    data = pickle.dumps(add5)
    add5_restored = pickle.loads(data)

    assert isinstance(add5_restored, StFunction)
    with pytest.raises(RuntimeError, match="not active"):
        add5_restored(3)

    # Activate and verify closure value survived
    sandbox.activate(add5_restored)
    assert add5_restored(3) == 8
    assert add5_restored(10) == 15


def test_sbfunction_closure_multiple_vars():
    """Multiple closure variables are all frozen."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
def make_fn(a, b, c):
    def compute(x):
        return a * x * x + b * x + c
    return compute

quadratic = make_fn(2, 3, 1)
""")
    assert result.error is None
    f = result.namespace["quadratic"]
    assert f(5) == 2 * 25 + 3 * 5 + 1  # 66

    data = pickle.dumps(f)
    f2 = pickle.loads(data)
    sandbox.activate(f2)
    assert f2(5) == 66


def test_sbfunction_multiple_functions():
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
def add(a, b): return a + b
def mul(a, b): return a * b
result = add(2, 3) + mul(4, 5)
""")
    assert result.error is None
    assert result.namespace["result"] == 25
    assert isinstance(result.namespace["add"], StFunction)
    assert isinstance(result.namespace["mul"], StFunction)


def test_sbfunction_with_class_method():
    """Methods inside classes should work in wrapped mode."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class Calculator:
    def add(self, a, b):
        return a + b

c = Calculator()
result = c.add(3, 4)
""")
    assert result.error is None
    assert result.namespace["result"] == 7


# --- StClass / StInstance tests ---


def test_wrapped_mode_creates_sbclass():
    """Classes in wrapped mode are wrapped in StClass."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("class Foo: pass")
    assert result.error is None
    assert isinstance(result.namespace["Foo"], StClass)


def test_raw_mode_creates_regular_class():
    """Classes in raw mode are plain types."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="raw")
    result = sandbox.exec("class Foo: pass")
    assert result.error is None
    assert isinstance(result.namespace["Foo"], type)


def test_sbclass_instantiation():
    """StClass.__call__ creates StInstance."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

p = Point(3, 4)
result = p.x + p.y
""")
    assert result.error is None
    assert result.namespace["result"] == 7
    assert isinstance(result.namespace["p"], StInstance)


def test_sbclass_methods():
    """Methods on StInstance delegate to the real instance."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class Counter:
    def __init__(self):
        self.n = 0
    def inc(self):
        self.n += 1
    def value(self):
        return self.n

c = Counter()
c.inc()
c.inc()
c.inc()
result = c.value()
""")
    assert result.error is None
    assert result.namespace["result"] == 3


def test_sbclass_dunders():
    """Dunder methods on StInstance work via forwarding."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class MyList:
    def __init__(self):
        self.items = [1, 2, 3]
    def __len__(self):
        return len(self.items)
    def __str__(self):
        return str(self.items)

ml = MyList()
length = len(ml)
s = str(ml)
""")
    assert result.error is None
    assert result.namespace["length"] == 3
    assert result.namespace["s"] == "[1, 2, 3]"


def test_sbclass_pickle_roundtrip():
    """StClass can be pickled and reactivated."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class Adder:
    def __init__(self, n):
        self.n = n
    def add(self, x):
        return self.n + x
""")
    assert result.error is None
    cls = result.namespace["Adder"]

    data = pickle.dumps(cls)
    cls2 = pickle.loads(data)
    assert isinstance(cls2, StClass)

    # Activate and use
    sandbox.activate(cls2)
    obj = cls2(10)
    assert isinstance(obj, StInstance)
    assert obj.add(5) == 15


def test_sbinstance_pickle_roundtrip():
    """StInstance can be pickled and reactivated."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def magnitude(self):
        return (self.x ** 2 + self.y ** 2) ** 0.5

p = Point(3, 4)
""")
    assert result.error is None
    p = result.namespace["p"]
    assert p.x == 3

    data = pickle.dumps(p)
    p2 = pickle.loads(data)
    assert isinstance(p2, StInstance)

    # Activate (also activates the class)
    sandbox.activate(p2)
    assert p2.x == 3
    assert p2.y == 4
    assert p2.magnitude() == 5.0


def test_sbclass_with_decorator():
    """Class decorators work and refs are frozen for recompilation."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
def add_greet(cls):
    cls.greet = lambda self: f"Hello from {self.name}"
    return cls

@add_greet
class Person:
    def __init__(self, name):
        self.name = name

p = Person("Alice")
result = p.greet()
""")
    assert result.error is None
    assert result.namespace["result"] == "Hello from Alice"

    # Pickle round-trip
    cls = result.namespace["Person"]
    data = pickle.dumps(cls)
    cls2 = pickle.loads(data)
    sandbox.activate(cls2, namespace={"add_greet": result.namespace["add_greet"]})
    p2 = cls2("Bob")
    assert p2.greet() == "Hello from Bob"


def test_sbclass_with_inheritance():
    """Base class references are frozen for recompilation."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
class Base:
    def hello(self):
        return "hello"

class Child(Base):
    def world(self):
        return "world"

c = Child()
result = c.hello() + " " + c.world()
""")
    assert result.error is None
    assert result.namespace["result"] == "hello world"


def test_sbfunction_lambda_not_wrapped():
    """Lambdas are not wrapped (no FunctionDef)."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("f = lambda x: x + 1\nresult = f(5)")
    assert result.error is None
    assert result.namespace["result"] == 6
    # Lambda is NOT an StFunction (it's a Lambda, not FunctionDef)
    assert not isinstance(result.namespace["f"], StFunction)


def test_sbfunction_decorated():
    """Decorated functions get wrapped after decoration."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")
    result = sandbox.exec("""\
def decorator(fn):
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs) * 2
    return wrapper

@decorator
def f(x):
    return x + 1

result = f(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 12
    # f is wrapped by __st_defun__ AFTER decoration
    assert isinstance(result.namespace["f"], StFunction)


def test_sbclass_class_level_attribute():
    """StClass proxies class-level attribute access."""
    sandbox = Sandbox(Policy(), mode="wrapped")
    result = sandbox.exec("""\
class Foo:
    X = 42
val = Foo.X
""")
    assert result.error is None
    assert result.namespace["val"] == 42


def test_sbclass_static_method():
    """StClass proxies static method access."""
    sandbox = Sandbox(Policy(), mode="wrapped")
    result = sandbox.exec("""\
class Calc:
    @staticmethod
    def add(a, b):
        return a + b
result = Calc.add(3, 4)
""")
    assert result.error is None
    assert result.namespace["result"] == 7


def test_sbclass_class_method():
    """StClass proxies class method access."""
    sandbox = Sandbox(Policy(), mode="wrapped")
    result = sandbox.exec("""\
class Counter:
    count = 0
    @classmethod
    def increment(cls):
        cls.count += 1
        return cls.count
result = Counter.increment()
""")
    assert result.error is None
    assert result.namespace["result"] == 1


def test_sbinstance_protocol_dunders_work():
    """StInstance protocol dunder forwarders work directly (bypass gate)."""
    sandbox = Sandbox(Policy(), mode="wrapped")
    result = sandbox.exec("""\
class MyList:
    def __init__(self, items):
        self.items = items
    def __len__(self):
        return len(self.items)
    def __iter__(self):
        return iter(self.items)
    def __contains__(self, item):
        return item in self.items

obj = MyList([1, 2, 3])
length = len(obj)
items = list(obj)
has_two = 2 in obj
""")
    assert result.error is None
    assert result.namespace["length"] == 3
    assert result.namespace["items"] == [1, 2, 3]
    assert result.namespace["has_two"] is True


def test_sbinstance_operators_work():
    """StInstance arithmetic/comparison dunder forwarders work."""
    sandbox = Sandbox(Policy(), mode="wrapped")
    result = sandbox.exec("""\
class Pair:
    def __init__(self, a, b):
        self.a = a
        self.b = b
    def __add__(self, other):
        return Pair(self.a + other.a, self.b + other.b)
    def __eq__(self, other):
        return self.a == other.a and self.b == other.b
    def __str__(self):
        return f'({self.a}, {self.b})'

p = Pair(1, 2) + Pair(3, 4)
s = str(p)
eq = p == Pair(4, 6)
""")
    assert result.error is None
    assert result.namespace["s"] == "(4, 6)"
    assert result.namespace["eq"] is True


# --- Auto-activation tests ---


def test_auto_activate_sbfunction_in_namespace():
    """Inactive StFunction passed via namespace is auto-activated by exec()."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")

    # Define and pickle a function
    result = sandbox.exec("def inc(x): return x + 1")
    f = result.namespace["inc"]
    data = pickle.dumps(f)
    f2 = pickle.loads(data)
    assert f2._compiled is None  # Inactive

    # Pass inactive wrapper in namespace — exec should auto-activate it
    result2 = sandbox.exec("y = inc(10)", namespace={"inc": f2})
    assert result2.error is None
    assert result2.namespace["y"] == 11


def test_auto_activate_sbclass_in_namespace():
    """Inactive StClass passed via namespace is auto-activated by exec()."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def sum(self):
        return self.x + self.y
""")
    cls = result.namespace["Point"]
    data = pickle.dumps(cls)
    cls2 = pickle.loads(data)
    assert cls2._compiled_cls is None  # Inactive

    result2 = sandbox.exec("p = Point(3, 7)\nr = p.sum()", namespace={"Point": cls2})
    assert result2.error is None
    assert result2.namespace["r"] == 10


def test_auto_activate_sbinstance_in_namespace():
    """Inactive StInstance passed via namespace is auto-activated by exec()."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
class Counter:
    def __init__(self, n):
        self.n = n
    def value(self):
        return self.n

c = Counter(42)
""")
    c = result.namespace["c"]
    data = pickle.dumps(c)
    c2 = pickle.loads(data)

    result2 = sandbox.exec("v = c.value()", namespace={"c": c2})
    assert result2.error is None
    assert result2.namespace["v"] == 42


def test_auto_activate_multiple_wrappers():
    """Multiple inactive wrappers are all auto-activated."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def add(a, b): return a + b
def mul(a, b): return a * b
""")
    add_fn = result.namespace["add"]
    mul_fn = result.namespace["mul"]

    # Pickle both
    add2 = pickle.loads(pickle.dumps(add_fn))
    mul2 = pickle.loads(pickle.dumps(mul_fn))

    result2 = sandbox.exec(
        "r = add(2, 3) + mul(4, 5)",
        namespace={"add": add2, "mul": mul2},
    )
    assert result2.error is None
    assert result2.namespace["r"] == 25


# --- _call_in_context tests (direct calls with sandbox protections) ---


def test_direct_call_enforces_tick_limit():
    """Direct StFunction call respects tick_limit via _call_in_context."""
    policy = Policy(tick_limit=50)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def count_high():
    total = 0
    for i in range(200):
        total += i
    return total
""")
    assert result.error is None
    f = result.namespace["count_high"]

    with pytest.raises(StTickLimit):
        f()


def test_direct_call_enforces_timeout():
    """Direct StFunction call respects timeout via _call_in_context."""
    policy = Policy(timeout=0.1)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def spin():
    while True:
        pass
""")
    assert result.error is None
    f = result.namespace["spin"]

    with pytest.raises(StTimeout):
        f()


def test_direct_call_enforces_cancellation():
    """Direct StFunction call respects cancellation via _call_in_context."""
    policy = Policy(timeout=10.0)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def spin():
    while True:
        pass
""")
    assert result.error is None
    f = result.namespace["spin"]

    # Cancel from a timer thread after a short delay
    timer = threading.Timer(0.05, sandbox.cancel)
    timer.start()

    with pytest.raises(StCancelled):
        f()
    timer.cancel()


def test_direct_call_resets_tick_counter():
    """Each direct call gets a fresh tick counter."""
    policy = Policy(tick_limit=100)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def loop50():
    for i in range(50):
        pass
""")
    assert result.error is None
    f = result.namespace["loop50"]

    # Both calls should succeed — each starts fresh at 0 ticks
    f()
    f()


def test_direct_call_sbclass_enforces_tick_limit():
    """Direct StClass construction respects tick_limit."""
    policy = Policy(tick_limit=50)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
class Heavy:
    def __init__(self):
        for i in range(200):
            pass
""")
    assert result.error is None
    cls = result.namespace["Heavy"]

    with pytest.raises(StTickLimit):
        cls()


def test_direct_call_after_pickle_roundtrip():
    """Pickled + activated StFunction gets full sandbox context on direct call."""
    policy = Policy(tick_limit=50)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def count_high():
    for i in range(200):
        pass
""")
    f = result.namespace["count_high"]

    # Pickle round-trip
    f2 = pickle.loads(pickle.dumps(f))
    sandbox.activate(f2)

    # Direct call should enforce tick limit
    with pytest.raises(StTickLimit):
        f2()


def test_direct_call_succeeds_under_limits():
    """Direct StFunction call succeeds when within limits."""
    policy = Policy(tick_limit=1000, timeout=10.0)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def compute(n):
    total = 0
    for i in range(n):
        total += i
    return total
""")
    assert result.error is None
    f = result.namespace["compute"]

    assert f(10) == 45
    assert f(50) == 1225


# --- Frozen globals tests ---


def test_frozen_globals_on_pickle():
    """Global StFunction deps are frozen on pickle and restored on activate."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def square(x): return x * x
def sum_squares(lst):
    return sum(square(x) for x in lst)
""")
    assert result.error is None
    sum_sq = result.namespace["sum_squares"]

    # Pickle only sum_squares (not square)
    data = pickle.dumps(sum_sq)
    restored = pickle.loads(data)

    # _frozen_globals should contain square
    assert hasattr(restored, "_frozen_globals")
    assert "square" in restored._frozen_globals
    assert isinstance(restored._frozen_globals["square"], StFunction)

    # Activate without providing square — frozen globals make it work
    sandbox.activate(restored)
    assert restored([1, 2, 3]) == 14


def test_frozen_globals_namespace_overrides():
    """Caller namespace overrides frozen globals (late binding)."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def square(x): return x * x
def apply(x):
    return square(x)
""")
    assert result.error is None
    apply_fn = result.namespace["apply"]

    # Pickle and restore
    restored = pickle.loads(pickle.dumps(apply_fn))

    # Define a different "square" — cubes instead
    result2 = sandbox.exec("def cube(x): return x * x * x")
    cube_fn = result2.namespace["cube"]

    # Activate with namespace override
    sandbox.activate(restored, namespace={"square": cube_fn})
    # Should use the namespace version (cube), not the frozen one
    assert restored(3) == 27


def test_frozen_globals_closure_wins_over_globals():
    """Closure vars take priority over frozen globals with the same name."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def helper(x): return x + 100

def make_fn(n):
    def f(x):
        return x + n
    return f

add5 = make_fn(5)
""")
    assert result.error is None
    add5 = result.namespace["add5"]

    # Pickle round-trip
    restored = pickle.loads(pickle.dumps(add5))
    sandbox.activate(restored)

    # Closure value n=5 should be preserved
    assert restored(10) == 15


def test_global_refs_property():
    """global_refs returns names of global references."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def square(x): return x * x
def sum_squares(lst):
    return sum(square(x) for x in lst)
""")
    assert result.error is None
    sum_sq = result.namespace["sum_squares"]

    refs = sum_sq.global_refs
    assert "square" in refs


def test_global_refs_property_after_pickle():
    """global_refs works after pickle round-trip."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def square(x): return x * x
def sum_squares(lst):
    return sum(square(x) for x in lst)
""")
    assert result.error is None
    sum_sq = result.namespace["sum_squares"]

    restored = pickle.loads(pickle.dumps(sum_sq))
    refs = restored.global_refs
    assert "square" in refs


def test_frozen_globals_auto_activate():
    """Frozen globals are auto-activated when the parent is activated."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    result = sandbox.exec("""\
def square(x): return x * x
def sum_squares(lst):
    return sum(square(x) for x in lst)
""")
    assert result.error is None
    sum_sq = result.namespace["sum_squares"]

    # Pickle and restore only sum_squares
    restored = pickle.loads(pickle.dumps(sum_sq))
    assert restored._frozen_globals["square"]._compiled is None  # Inactive

    # Activate sum_squares — should auto-activate frozen square
    sandbox.activate(restored)
    assert restored([1, 2, 3, 4]) == 30
