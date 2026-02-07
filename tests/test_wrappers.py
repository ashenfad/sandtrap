"""Tests for SbFunction and SbClass wrappers."""

import pickle

import pytest

from sblite import Policy, Sandbox, SbClass, SbFunction, SbInstance


def test_task_mode_creates_sbfunction():
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("def f(x): return x + 1")
    assert result.error is None
    assert isinstance(result.namespace["f"], SbFunction)


def test_service_mode_creates_regular_function():
    policy = Policy()
    sandbox = Sandbox(policy, mode="service")
    result = sandbox.exec("def f(x): return x + 1")
    assert result.error is None
    assert not isinstance(result.namespace["f"], SbFunction)
    assert callable(result.namespace["f"])


def test_sbfunction_callable():
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("def double(x): return x * 2")
    assert result.error is None
    f = result.namespace["double"]
    assert f(5) == 10


def test_sbfunction_metadata():
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("def f(x): return x + 1")
    f = result.namespace["f"]

    # Pickle and unpickle
    data = pickle.dumps(f)
    f2 = pickle.loads(data)

    # Should be inactive after unpickle
    assert isinstance(f2, SbFunction)
    with pytest.raises(RuntimeError, match="not active"):
        f2(1)

    # Activate and use
    sandbox.activate(f2)
    assert f2(2) == 3


def test_sbfunction_repr():
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("def f(x): return x")
    f = result.namespace["f"]
    assert "active" in repr(f)

    data = pickle.dumps(f)
    f2 = pickle.loads(data)
    assert "inactive" in repr(f2)


def test_sbfunction_with_defaults():
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("def add(a, b=10): return a + b")
    f = result.namespace["add"]
    assert f(5) == 15
    assert f(5, 20) == 25


def test_sbfunction_with_closure():
    """Functions that capture local scope variables."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    # Inner function is also an SbFunction
    assert isinstance(result.namespace["add5"], SbFunction)


def test_sbfunction_closure_pickle_roundtrip():
    """Closure variables are frozen and restored on pickle round-trip."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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

    assert isinstance(add5_restored, SbFunction)
    with pytest.raises(RuntimeError, match="not active"):
        add5_restored(3)

    # Activate and verify closure value survived
    sandbox.activate(add5_restored)
    assert add5_restored(3) == 8
    assert add5_restored(10) == 15


def test_sbfunction_closure_multiple_vars():
    """Multiple closure variables are all frozen."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("""\
def add(a, b): return a + b
def mul(a, b): return a * b
result = add(2, 3) + mul(4, 5)
""")
    assert result.error is None
    assert result.namespace["result"] == 25
    assert isinstance(result.namespace["add"], SbFunction)
    assert isinstance(result.namespace["mul"], SbFunction)


def test_sbfunction_with_class_method():
    """Methods inside classes should work in task mode."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("""\
class Calculator:
    def add(self, a, b):
        return a + b

c = Calculator()
result = c.add(3, 4)
""")
    assert result.error is None
    assert result.namespace["result"] == 7


# --- SbClass / SbInstance tests ---


def test_task_mode_creates_sbclass():
    """Classes in task mode are wrapped in SbClass."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("class Foo: pass")
    assert result.error is None
    assert isinstance(result.namespace["Foo"], SbClass)


def test_service_mode_creates_regular_class():
    """Classes in service mode are plain types."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="service")
    result = sandbox.exec("class Foo: pass")
    assert result.error is None
    assert isinstance(result.namespace["Foo"], type)


def test_sbclass_instantiation():
    """SbClass.__call__ creates SbInstance."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    assert isinstance(result.namespace["p"], SbInstance)


def test_sbclass_methods():
    """Methods on SbInstance delegate to the real instance."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    """Dunder methods on SbInstance work via forwarding."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    """SbClass can be pickled and reactivated."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    assert isinstance(cls2, SbClass)

    # Activate and use
    sandbox.activate(cls2)
    obj = cls2(10)
    assert isinstance(obj, SbInstance)
    assert obj.add(5) == 15


def test_sbinstance_pickle_roundtrip():
    """SbInstance can be pickled and reactivated."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    assert isinstance(p2, SbInstance)

    # Activate (also activates the class)
    sandbox.activate(p2)
    assert p2.x == 3
    assert p2.y == 4
    assert p2.magnitude() == 5.0


def test_sbclass_with_decorator():
    """Class decorators work and refs are frozen for recompilation."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    sandbox = Sandbox(policy, mode="task")
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
    sandbox = Sandbox(policy, mode="task")
    result = sandbox.exec("f = lambda x: x + 1\nresult = f(5)")
    assert result.error is None
    assert result.namespace["result"] == 6
    # Lambda is NOT an SbFunction (it's a Lambda, not FunctionDef)
    assert not isinstance(result.namespace["f"], SbFunction)


def test_sbfunction_decorated():
    """Decorated functions get wrapped after decoration."""
    policy = Policy()
    sandbox = Sandbox(policy, mode="task")
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
    # f is wrapped by __sb_defun__ AFTER decoration
    assert isinstance(result.namespace["f"], SbFunction)


def test_sbclass_class_level_attribute():
    """SbClass proxies class-level attribute access."""
    sandbox = Sandbox(Policy(), mode="task")
    result = sandbox.exec("""\
class Foo:
    X = 42
val = Foo.X
""")
    assert result.error is None
    assert result.namespace["val"] == 42


def test_sbclass_static_method():
    """SbClass proxies static method access."""
    sandbox = Sandbox(Policy(), mode="task")
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
    """SbClass proxies class method access."""
    sandbox = Sandbox(Policy(), mode="task")
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
    """SbInstance protocol dunder forwarders work directly (bypass gate)."""
    sandbox = Sandbox(Policy(), mode="task")
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
    """SbInstance arithmetic/comparison dunder forwarders work."""
    sandbox = Sandbox(Policy(), mode="task")
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
