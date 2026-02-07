"""Stress tests: complex but valid code that should work correctly."""

from sblite import Policy, Sandbox


def sandbox():
    return Sandbox(Policy(timeout=5.0, tick_limit=100_000))


# --- Closures ---


def test_closure_in_loop():
    sbx = sandbox()
    result = sbx.exec("""\
funcs = []
for i in range(5):
    def make(n):
        def f():
            return n
        return f
    funcs.append(make(i))
results = [f() for f in funcs]
""")
    assert result.error is None
    assert result.namespace["results"] == [0, 1, 2, 3, 4]


def test_closure_late_binding_gotcha():
    """Classic Python closure gotcha — all closures see final value."""
    sbx = sandbox()
    result = sbx.exec("""\
funcs = []
for i in range(5):
    funcs.append(lambda: i)
results = [f() for f in funcs]
""")
    assert result.error is None
    assert result.namespace["results"] == [4, 4, 4, 4, 4]


def test_nested_closures():
    sbx = sandbox()
    result = sbx.exec("""\
def outer(x):
    def middle(y):
        def inner(z):
            return x + y + z
        return inner
    return middle
result = outer(1)(2)(3)
""")
    assert result.error is None
    assert result.namespace["result"] == 6


# --- Generators ---


def test_generator_with_send():
    sbx = sandbox()
    result = sbx.exec("""\
def accumulator():
    total = 0
    while True:
        value = yield total
        if value is None:
            break
        total += value

gen = accumulator()
next(gen)
gen.send(10)
gen.send(20)
result = gen.send(30)
""")
    assert result.error is None
    assert result.namespace["result"] == 60


def test_generator_expression_chain():
    sbx = sandbox()
    result = sbx.exec("""\
nums = range(20)
evens = (x for x in nums if x % 2 == 0)
squares = (x * x for x in evens)
result = list(squares)
""")
    assert result.error is None
    assert result.namespace["result"] == [0, 4, 16, 36, 64, 100, 144, 196, 256, 324]


def test_yield_from():
    sbx = sandbox()
    result = sbx.exec("""\
def inner():
    yield 1
    yield 2

def outer():
    yield 0
    yield from inner()
    yield 3

result = list(outer())
""")
    assert result.error is None
    assert result.namespace["result"] == [0, 1, 2, 3]


# --- Decorators ---


def test_stacked_decorators():
    sbx = sandbox()
    result = sbx.exec("""\
def double(fn):
    def wrapper(*a, **kw):
        return fn(*a, **kw) * 2
    return wrapper

def add_one(fn):
    def wrapper(*a, **kw):
        return fn(*a, **kw) + 1
    return wrapper

@add_one
@double
def f(x):
    return x

result = f(5)
""")
    assert result.error is None
    assert result.namespace["result"] == 11  # (5 * 2) + 1


def test_decorator_with_args():
    sbx = sandbox()
    result = sbx.exec("""\
def multiply(factor):
    def decorator(fn):
        def wrapper(*a, **kw):
            return fn(*a, **kw) * factor
        return wrapper
    return decorator

@multiply(3)
def f(x):
    return x + 1

result = f(4)
""")
    assert result.error is None
    assert result.namespace["result"] == 15


# --- Classes ---


def test_multiple_inheritance():
    sbx = sandbox()
    result = sbx.exec("""\
class A:
    def greet(self):
        return "A"

class B:
    def farewell(self):
        return "B"

class C(A, B):
    pass

c = C()
result = c.greet() + c.farewell()
""")
    assert result.error is None
    assert result.namespace["result"] == "AB"


def test_property_via_descriptor():
    sbx = sandbox()
    # Note: _radius uses underscore prefix which is private.
    # Sandbox-defined classes access their own attrs internally
    # via self.radius (no underscore) to avoid the attr gate.
    result = sbx.exec("""\
class Circle:
    def __init__(self, radius):
        self.radius = radius

    @property
    def area(self):
        return 3.14159 * self.radius ** 2

c = Circle(5)
result = c.area
""")
    assert result.error is None
    assert abs(result.namespace["result"] - 78.53975) < 0.001


def test_class_with_dunder_methods():
    sbx = sandbox()
    result = sbx.exec("""\
class Vec:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def __add__(self, other):
        return Vec(self.x + other.x, self.y + other.y)
    def __eq__(self, other):
        return self.x == other.x and self.y == other.y
    def __repr__(self):
        return f"Vec({self.x}, {self.y})"

v = Vec(1, 2) + Vec(3, 4)
result = repr(v)
eq = v == Vec(4, 6)
""")
    assert result.error is None
    assert result.namespace["result"] == "Vec(4, 6)"
    assert result.namespace["eq"] is True


def test_nested_classes():
    sbx = sandbox()
    result = sbx.exec("""\
class Outer:
    class Inner:
        val = 42
    def get(self):
        return self.Inner.val

result = Outer().get()
""")
    assert result.error is None
    assert result.namespace["result"] == 42


# --- Unpacking ---


def test_star_unpacking():
    sbx = sandbox()
    result = sbx.exec("""\
first, *rest, last = [1, 2, 3, 4, 5]
""")
    assert result.error is None
    assert result.namespace["first"] == 1
    assert result.namespace["rest"] == [2, 3, 4]
    assert result.namespace["last"] == 5


def test_nested_unpacking():
    sbx = sandbox()
    result = sbx.exec("""\
(a, b), (c, d) = [1, 2], [3, 4]
""")
    assert result.error is None
    assert result.namespace["a"] == 1
    assert result.namespace["d"] == 4


def test_dict_unpacking():
    sbx = sandbox()
    result = sbx.exec("""\
a = {"x": 1}
b = {"y": 2}
c = {**a, **b, "z": 3}
""")
    assert result.error is None
    assert result.namespace["c"] == {"x": 1, "y": 2, "z": 3}


# --- Walrus operator ---


def test_walrus_in_while():
    sbx = sandbox()
    result = sbx.exec("""\
data = [1, 2, 3, 0, 4, 5]
it = iter(data)
results = []
while (val := next(it, None)) is not None:
    if val == 0:
        break
    results.append(val)
""")
    assert result.error is None
    assert result.namespace["results"] == [1, 2, 3]


def test_walrus_in_comprehension():
    sbx = sandbox()
    result = sbx.exec("""\
result = [y for x in range(5) if (y := x * x) > 5]
""")
    assert result.error is None
    assert result.namespace["result"] == [9, 16]


# --- Context managers ---


def test_custom_context_manager():
    sbx = sandbox()
    result = sbx.exec("""\
class CM:
    def __init__(self):
        self.entered = False
        self.exited = False
    def __enter__(self):
        self.entered = True
        return self
    def __exit__(self, *args):
        self.exited = True

cm = CM()
with cm as c:
    inside = c.entered
exited = cm.exited
""")
    assert result.error is None
    assert result.namespace["inside"] is True
    assert result.namespace["exited"] is True


# --- Exception handling ---


def test_try_except_finally():
    sbx = sandbox()
    result = sbx.exec("""\
log = []
try:
    log.append("try")
    x = 1 / 0
except ZeroDivisionError:
    log.append("except")
finally:
    log.append("finally")
""")
    assert result.error is None
    assert result.namespace["log"] == ["try", "except", "finally"]


def test_exception_chaining():
    """Exception chaining works; __cause__ is a dunder blocked by attr gate."""
    sbx = sandbox()
    result = sbx.exec("""\
try:
    try:
        raise ValueError("inner")
    except ValueError as e:
        raise TypeError("outer") from e
except TypeError as e:
    result = str(e)
""")
    assert result.error is None
    assert result.namespace["result"] == "outer"


def test_reraise():
    sbx = sandbox()
    result = sbx.exec("""\
caught = False
try:
    try:
        raise ValueError("test")
    except ValueError:
        caught = True
        raise
except ValueError:
    pass
""")
    assert result.error is None
    assert result.namespace["caught"] is True


# --- Match/case ---


def test_match_case():
    sbx = sandbox()
    result = sbx.exec("""\
def classify(x):
    match x:
        case 0:
            return "zero"
        case int() if x > 0:
            return "positive"
        case int():
            return "negative"
        case str():
            return "string"
        case _:
            return "other"

results = [classify(0), classify(5), classify(-3), classify("hi"), classify([])]
""")
    assert result.error is None
    assert result.namespace["results"] == ["zero", "positive", "negative", "string", "other"]


# --- Complex comprehensions ---


def test_nested_comprehension():
    sbx = sandbox()
    result = sbx.exec("""\
matrix = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
flat = [x for row in matrix for x in row]
""")
    assert result.error is None
    assert result.namespace["flat"] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_dict_comprehension():
    sbx = sandbox()
    result = sbx.exec("""\
result = {k: v for k, v in enumerate("abcde")}
""")
    assert result.error is None
    assert result.namespace["result"] == {0: "a", 1: "b", 2: "c", 3: "d", 4: "e"}


def test_set_comprehension():
    sbx = sandbox()
    result = sbx.exec("""\
result = {x % 3 for x in range(10)}
""")
    assert result.error is None
    assert result.namespace["result"] == {0, 1, 2}


# --- Mixed complex patterns ---


def test_recursive_data_structure():
    sbx = sandbox()
    result = sbx.exec("""\
def tree(val, left=None, right=None):
    return {"val": val, "left": left, "right": right}

def sum_tree(node):
    if node is None:
        return 0
    return node["val"] + sum_tree(node["left"]) + sum_tree(node["right"])

t = tree(1, tree(2, tree(4), tree(5)), tree(3))
result = sum_tree(t)
""")
    assert result.error is None
    assert result.namespace["result"] == 15


def test_fibonacci_generator():
    sbx = sandbox()
    result = sbx.exec("""\
def fib():
    a, b = 0, 1
    while True:
        yield a
        a, b = b, a + b

gen = fib()
result = [next(gen) for _ in range(10)]
""")
    assert result.error is None
    assert result.namespace["result"] == [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]


def test_memoized_function():
    sbx = sandbox()
    result = sbx.exec("""\
def memoize(fn):
    cache = {}
    def wrapper(n):
        if n not in cache:
            cache[n] = fn(n)
        return cache[n]
    return wrapper

@memoize
def fib(n):
    if n <= 1:
        return n
    return fib(n-1) + fib(n-2)

result = fib(30)
""")
    assert result.error is None
    assert result.namespace["result"] == 832040
