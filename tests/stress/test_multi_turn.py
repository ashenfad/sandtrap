"""Stress tests: multi-turn workflows with pickle round-trips."""

import pickle

import pytest

from sandtrap import MemoryFS, Policy, Sandbox, find_refs
from sandtrap.errors import StTickLimit
from sandtrap.wrappers import StClass, StFunction

# --- Function pickle + reuse ---


def test_function_pickle_and_call_in_second_turn():
    """Define fn in turn 1, pickle, use in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1
    r1 = sandbox.exec("def double(x): return x * 2")
    assert r1.error is None
    data = pickle.dumps(r1.namespace["double"])

    # Turn 2
    fn = pickle.loads(data)
    r2 = sandbox.exec("result = double(21)", namespace={"double": fn})
    assert r2.error is None
    assert r2.namespace["result"] == 42


def test_closure_survives_pickle():
    """Closure variable frozen across pickle boundary."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def make_adder(n):
    def add(x):
        return x + n
    return add
add10 = make_adder(10)
""")
    assert r1.error is None
    data = pickle.dumps(r1.namespace["add10"])

    fn = pickle.loads(data)
    r2 = sandbox.exec("result = add10(5)", namespace={"add10": fn})
    assert r2.error is None
    assert r2.namespace["result"] == 15


def test_multiple_functions_across_turns():
    """Multiple functions defined in turn 1, all used in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def add(a, b): return a + b
def mul(a, b): return a * b
def sub(a, b): return a - b
""")
    assert r1.error is None

    ns = {}
    for name in ("add", "mul", "sub"):
        ns[name] = pickle.loads(pickle.dumps(r1.namespace[name]))

    r2 = sandbox.exec("result = add(mul(3, 4), sub(10, 5))", namespace=ns)
    assert r2.error is None
    assert r2.namespace["result"] == 17


def test_function_defined_in_turn2_uses_turn1_fn():
    """Turn 2 defines a new function that calls a turn 1 function."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("def square(x): return x * x")
    assert r1.error is None
    sq = pickle.loads(pickle.dumps(r1.namespace["square"]))

    r2 = sandbox.exec("""\
def sum_of_squares(a, b):
    return square(a) + square(b)
result = sum_of_squares(3, 4)
""", namespace={"square": sq})
    assert r2.error is None
    assert r2.namespace["result"] == 25


# --- Class pickle + reuse ---


def test_class_pickle_and_construct_in_second_turn():
    """Define class in turn 1, pickle, construct in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Counter:
    def __init__(self, start=0):
        self.n = start
    def inc(self):
        self.n += 1
    def value(self):
        return self.n
""")
    assert r1.error is None
    data = pickle.dumps(r1.namespace["Counter"])

    cls = pickle.loads(data)
    r2 = sandbox.exec("""\
c = Counter(10)
c.inc()
c.inc()
result = c.value()
""", namespace={"Counter": cls})
    assert r2.error is None
    assert r2.namespace["result"] == 12


def test_instance_pickle_across_turns():
    """Construct instance in turn 1, pickle, use in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Accum:
    def __init__(self):
        self.total = 0
    def add(self, x):
        self.total += x
    def value(self):
        return self.total

a = Accum()
a.add(10)
a.add(20)
""")
    assert r1.error is None
    data = pickle.dumps(r1.namespace["a"])

    inst = pickle.loads(data)
    r2 = sandbox.exec("""\
a.add(30)
result = a.value()
""", namespace={"a": inst})
    assert r2.error is None
    assert r2.namespace["result"] == 60


def test_class_with_inheritance_across_turns():
    """Base + derived class both pickle and work in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Base:
    def greet(self):
        return "hello"

class Child(Base):
    def farewell(self):
        return "bye"
""")
    assert r1.error is None
    ns = {
        "Base": pickle.loads(pickle.dumps(r1.namespace["Base"])),
        "Child": pickle.loads(pickle.dumps(r1.namespace["Child"])),
    }

    r2 = sandbox.exec("""\
c = Child()
result = c.greet() + " " + c.farewell()
""", namespace=ns)
    assert r2.error is None
    assert r2.namespace["result"] == "hello bye"


# --- Mixed fn + class across turns ---


def test_function_operating_on_class_across_turns():
    """Turn 1 defines class + function, turn 2 uses both."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

def distance(p1, p2):
    return ((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2) ** 0.5
""")
    assert r1.error is None
    ns = {
        "Point": pickle.loads(pickle.dumps(r1.namespace["Point"])),
        "distance": pickle.loads(pickle.dumps(r1.namespace["distance"])),
    }

    r2 = sandbox.exec("""\
a = Point(0, 0)
b = Point(3, 4)
result = distance(a, b)
""", namespace=ns)
    assert r2.error is None
    assert r2.namespace["result"] == 5.0


# --- Three-turn chains ---


def test_three_turn_accumulation():
    """State accumulates across three turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define
    r1 = sandbox.exec("""\
class State:
    def __init__(self):
        self.items = []
    def add(self, x):
        self.items.append(x)
    def get(self):
        return list(self.items)

s = State()
s.add("turn1")
""")
    assert r1.error is None

    # Turn 2: continue
    ns2 = {
        "State": pickle.loads(pickle.dumps(r1.namespace["State"])),
        "s": pickle.loads(pickle.dumps(r1.namespace["s"])),
    }
    r2 = sandbox.exec('s.add("turn2")', namespace=ns2)
    assert r2.error is None

    # Turn 3: read
    ns3 = {
        "s": pickle.loads(pickle.dumps(r2.namespace["s"])),
    }
    r3 = sandbox.exec("result = s.get()", namespace=ns3)
    assert r3.error is None
    assert r3.namespace["result"] == ["turn1", "turn2"]


def test_function_evolution_across_turns():
    """Turn 1 defines helper, turn 2 defines fn using it, turn 3 calls it."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1
    r1 = sandbox.exec("def square(x): return x * x")
    assert r1.error is None

    # Turn 2: define new fn referencing turn 1 fn
    ns2 = {"square": pickle.loads(pickle.dumps(r1.namespace["square"]))}
    r2 = sandbox.exec("""\
def sum_squares(lst):
    return sum(square(x) for x in lst)
""", namespace=ns2)
    assert r2.error is None

    # Turn 3: use the composed fn (must include square — it's a global, not a closure var)
    ns3 = {
        "sum_squares": pickle.loads(pickle.dumps(r2.namespace["sum_squares"])),
        "square": pickle.loads(pickle.dumps(r1.namespace["square"])),
    }
    r3 = sandbox.exec("result = sum_squares([1, 2, 3, 4])", namespace=ns3)
    assert r3.error is None
    assert r3.namespace["result"] == 30


# --- Direct calls after pickle ---


def test_direct_call_after_pickle_has_sandbox_context():
    """Pickled + auto-activated fn gets sandbox protections on direct call."""
    policy = Policy(tick_limit=100)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def spin():
    while True:
        pass
""")
    assert r1.error is None
    fn = pickle.loads(pickle.dumps(r1.namespace["spin"]))

    # Auto-activate via exec, then extract and call directly
    r2 = sandbox.exec("x = 1", namespace={"spin": fn})
    assert r2.error is None

    spin = r2.namespace["spin"]
    with pytest.raises(StTickLimit):
        spin()


# --- Self-contained pickle (frozen globals) ---


def test_function_self_contained_after_pickle():
    """Pickled function works standalone via frozen globals."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def square(x): return x * x
def sum_squares(lst):
    return sum(square(x) for x in lst)
""")
    assert r1.error is None

    # Pickle only sum_squares
    data = pickle.dumps(r1.namespace["sum_squares"])
    restored = pickle.loads(data)

    # Activate standalone — frozen square makes it work
    sandbox.activate(restored)
    assert restored([1, 2, 3, 4]) == 30


def test_frozen_globals_override_by_namespace():
    """Namespace-provided dep overrides frozen global."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def square(x): return x * x
def apply(x):
    return square(x)
""")
    assert r1.error is None

    # Pickle apply
    data = pickle.dumps(r1.namespace["apply"])
    restored = pickle.loads(data)

    # Provide a different square in namespace
    r2 = sandbox.exec("result = apply(3)", namespace={
        "apply": restored,
        "square": r1.namespace["square"],  # original
    })
    assert r2.error is None
    assert r2.namespace["result"] == 9  # square(3) = 9

    # Now provide a cube function as "square"
    r3 = sandbox.exec("def cube(x): return x * x * x")
    restored2 = pickle.loads(data)
    r4 = sandbox.exec("result = apply(3)", namespace={
        "apply": restored2,
        "square": r3.namespace["cube"],  # override
    })
    assert r4.error is None
    assert r4.namespace["result"] == 27  # cube(3) = 27


# --- VFS module + pickle multi-turn ---


def test_vfs_module_function_survives_pickle():
    """VFS module function (StFunction in wrapped mode) survives pickle."""
    fs = MemoryFS()
    fs.files["/mathlib.py"] = b"def double(x): return x * 2"

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped", filesystem=fs)

    # Turn 1: import from VFS and define function that uses it
    r1 = sandbox.exec("""\
from mathlib import double
def quadruple(x):
    return double(double(x))
""")
    assert r1.error is None
    assert isinstance(r1.namespace["double"], StFunction)

    # Pickle only quadruple
    data = pickle.dumps(r1.namespace["quadruple"])
    restored = pickle.loads(data)

    # Activate standalone — frozen double (StFunction from VFS) makes it work
    sandbox.activate(restored)
    assert restored(5) == 20


def test_vfs_module_function_in_multi_turn():
    """VFS module dep available via frozen globals across turns."""
    fs = MemoryFS()
    fs.files["/utils.py"] = b"""\
def add(a, b): return a + b
def mul(a, b): return a * b
"""

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped", filesystem=fs)

    # Turn 1: import + define
    r1 = sandbox.exec("""\
from utils import add, mul
def dot(xs, ys):
    total = 0
    for x, y in zip(xs, ys):
        total = add(total, mul(x, y))
    return total
""")
    assert r1.error is None

    # Pickle the namespace
    ns_data = {k: pickle.dumps(v) for k, v in r1.namespace.items()
               if isinstance(v, StFunction)}

    # Turn 2: restore only dot and use it
    dot = pickle.loads(ns_data["dot"])
    r2 = sandbox.exec(
        "result = dot([1, 2, 3], [4, 5, 6])",
        namespace={"dot": dot},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 32  # 1*4 + 2*5 + 3*6


# --- Error recovery across turns ---


def test_error_in_turn_does_not_corrupt_prior_state():
    """Turn 2 errors, but turn 1 state is still usable in turn 3."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define function
    r1 = sandbox.exec("def double(x): return x * 2")
    assert r1.error is None
    data = pickle.dumps(r1.namespace["double"])

    # Turn 2: error
    fn = pickle.loads(data)
    r2 = sandbox.exec("result = double(1) + oops", namespace={"double": fn})
    assert r2.error is not None

    # Turn 3: prior state still works
    fn2 = pickle.loads(data)
    r3 = sandbox.exec("result = double(21)", namespace={"double": fn2})
    assert r3.error is None
    assert r3.namespace["result"] == 42


# --- Function redefinition across turns ---


def test_redefine_function_across_turns():
    """Turn 1 defines f, turn 2 redefines f, turn 3 uses new f."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: original
    r1 = sandbox.exec("def transform(x): return x * 2")
    assert r1.error is None
    data1 = pickle.dumps(r1.namespace["transform"])

    # Turn 2: redefine with different logic
    r2 = sandbox.exec("def transform(x): return x * x")
    assert r2.error is None
    data2 = pickle.dumps(r2.namespace["transform"])

    # Turn 3: uses the new definition
    fn = pickle.loads(data2)
    r3 = sandbox.exec("result = transform(5)", namespace={"transform": fn})
    assert r3.error is None
    assert r3.namespace["result"] == 25  # x*x, not x*2

    # Original still works independently
    fn_old = pickle.loads(data1)
    r4 = sandbox.exec("result = transform(5)", namespace={"transform": fn_old})
    assert r4.error is None
    assert r4.namespace["result"] == 10  # x*2


# --- Class method evolution via subclass ---


def test_subclass_overrides_method_across_turns():
    """Turn 1 defines base, turn 2 defines subclass with override, both pickle."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: base class
    r1 = sandbox.exec("""\
class Shape:
    def area(self):
        return 0
    def describe(self):
        return "shape"
""")
    assert r1.error is None

    # Turn 2: subclass overrides area
    ns2 = {"Shape": pickle.loads(pickle.dumps(r1.namespace["Shape"]))}
    r2 = sandbox.exec("""\
class Circle(Shape):
    def __init__(self, r):
        self.r = r
    def area(self):
        return 3.14 * self.r * self.r
""", namespace=ns2)
    assert r2.error is None

    # Turn 3: use subclass
    ns3 = {
        "Circle": pickle.loads(pickle.dumps(r2.namespace["Circle"])),
        "Shape": pickle.loads(pickle.dumps(r1.namespace["Shape"])),
    }
    r3 = sandbox.exec("""\
c = Circle(10)
a = c.area()
d = c.describe()
""", namespace=ns3)
    assert r3.error is None
    assert abs(r3.namespace["a"] - 314.0) < 0.1
    assert r3.namespace["d"] == "shape"  # inherited


# --- Instance from a later turn ---


def test_instance_constructed_in_later_turn():
    """Class defined turn 1, instance built turn 2, used turn 3."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define class only
    r1 = sandbox.exec("""\
class Bag:
    def __init__(self):
        self.items = []
    def add(self, x):
        self.items.append(x)
    def contents(self):
        return list(self.items)
""")
    assert r1.error is None

    # Turn 2: construct and populate
    ns2 = {"Bag": pickle.loads(pickle.dumps(r1.namespace["Bag"]))}
    r2 = sandbox.exec("""\
b = Bag()
b.add("alpha")
b.add("beta")
""", namespace=ns2)
    assert r2.error is None

    # Turn 3: use instance from turn 2 (without the class)
    ns3 = {"b": pickle.loads(pickle.dumps(r2.namespace["b"]))}
    r3 = sandbox.exec("result = b.contents()", namespace=ns3)
    assert r3.error is None
    assert r3.namespace["result"] == ["alpha", "beta"]


# --- Higher-order functions across turns ---


def test_higher_order_function_result_survives_pickle():
    """Apply a decorator in turn 1, pickle the result, use in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define decorator, apply it, pickle the result
    r1 = sandbox.exec("""\
def triple(fn):
    def wrapper(x):
        return fn(x) * 3
    return wrapper

def inc(x):
    return x + 1

tripled_inc = triple(inc)
""")
    assert r1.error is None

    # Pickle the composed result
    data = pickle.dumps(r1.namespace["tripled_inc"])
    restored = pickle.loads(data)

    # Turn 2: use the composed function
    r2 = sandbox.exec("result = tripled_inc(4)", namespace={"tripled_inc": restored})
    assert r2.error is None
    assert r2.namespace["result"] == 15  # (4 + 1) * 3


# --- Selective restore via find_refs ---


def test_selective_restore_via_find_refs():
    """Large namespace, only subset restored via find_refs, still works."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define many functions
    r1 = sandbox.exec("""\
def add(a, b): return a + b
def mul(a, b): return a * b
def sub(a, b): return a - b
def div(a, b): return a / b
def square(x): return mul(x, x)
def cube(x): return mul(x, mul(x, x))
def sum_squares(lst):
    return add(0, sum(square(x) for x in lst))
""")
    assert r1.error is None

    # Pickle everything
    pickled = {k: pickle.dumps(v) for k, v in r1.namespace.items()
               if isinstance(v, StFunction)}

    # Turn 2: only need sum_squares — use find_refs to discover deps
    source = "result = sum_squares([1, 2, 3])"
    all_pickled = {k: pickle.loads(v) for k, v in pickled.items()}
    refs = find_refs(source, namespace=all_pickled)

    # Should discover sum_squares, square, mul, add (transitive)
    assert "sum_squares" in refs
    assert "square" in refs
    assert "mul" in refs
    assert "add" in refs
    # Should NOT include unneeded functions
    assert "sub" not in refs
    assert "div" not in refs
    assert "cube" not in refs

    # Restore only what's needed
    ns = {k: pickle.loads(pickled[k]) for k in refs if k in pickled}
    r2 = sandbox.exec(source, namespace=ns)
    assert r2.error is None
    assert r2.namespace["result"] == 14  # 1 + 4 + 9


# --- Inner functions survive cross-turn activation ---


def test_factory_function_across_turns():
    """Factory with inner function defined in turn 1, called in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def make_adder(n):
    def add(x):
        return x + n
    return add
add10 = make_adder(10)
add20 = make_adder(20)
""")
    assert r1.error is None

    # Inner functions are StFunction (pickleable)
    assert isinstance(r1.namespace["add10"], StFunction)
    assert isinstance(r1.namespace["add20"], StFunction)

    # Pickle and restore in turn 2
    add10 = pickle.loads(pickle.dumps(r1.namespace["add10"]))
    add20 = pickle.loads(pickle.dumps(r1.namespace["add20"]))
    r2 = sandbox.exec("""\
result = add10(5) + add20(3)
""", namespace={"add10": add10, "add20": add20})
    assert r2.error is None
    assert r2.namespace["result"] == 38  # 15 + 23


def test_decorator_applied_across_turns():
    """Decorator + decorated function in turn 1, result used in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def triple(fn):
    def wrapper(x):
        return fn(x) * 3
    return wrapper

def inc(x):
    return x + 1

tripled = triple(inc)
""")
    assert r1.error is None
    assert isinstance(r1.namespace["tripled"], StFunction)

    data = pickle.dumps(r1.namespace["tripled"])
    restored = pickle.loads(data)

    r2 = sandbox.exec("result = tripled(4)", namespace={"tripled": restored})
    assert r2.error is None
    assert r2.namespace["result"] == 15  # (4 + 1) * 3


def test_nested_factory_across_turns():
    """Two-level nesting: factory of factories across turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def make_op(op):
    def apply(x, y):
        if op == "add":
            return x + y
        elif op == "mul":
            return x * y
    return apply

adder = make_op("add")
multiplier = make_op("mul")
""")
    assert r1.error is None

    adder = pickle.loads(pickle.dumps(r1.namespace["adder"]))
    multiplier = pickle.loads(pickle.dumps(r1.namespace["multiplier"]))

    r2 = sandbox.exec("""\
result = adder(multiplier(3, 4), 10)
""", namespace={"adder": adder, "multiplier": multiplier})
    assert r2.error is None
    assert r2.namespace["result"] == 22  # 3*4 + 10


# --- Async multi-turn ---


@pytest.mark.asyncio
async def test_async_function_pickle_across_turns():
    """Async function defined in turn 1, pickled, called via aexec in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
async def double(x):
    return x * 2
""")
    assert r1.error is None
    assert isinstance(r1.namespace["double"], StFunction)

    data = pickle.dumps(r1.namespace["double"])
    restored = pickle.loads(data)

    r2 = await sandbox.aexec(
        "result = await double(21)",
        namespace={"double": restored},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 42


@pytest.mark.asyncio
async def test_async_closure_survives_pickle():
    """Async closure (inner async def) survives pickle across turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
def make_async_adder(n):
    async def add(x):
        return x + n
    return add
add10 = make_async_adder(10)
""")
    assert r1.error is None
    assert isinstance(r1.namespace["add10"], StFunction)

    data = pickle.dumps(r1.namespace["add10"])
    restored = pickle.loads(data)

    r2 = await sandbox.aexec(
        "result = await add10(5)",
        namespace={"add10": restored},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 15


@pytest.mark.asyncio
async def test_async_calls_sync_dep_across_turns():
    """Async function calling a sync dep, both pickled across turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
def square(x):
    return x * x

async def sum_squares(lst):
    total = 0
    for x in lst:
        total = total + square(x)
    return total
""")
    assert r1.error is None

    # Pickle only sum_squares — square should be frozen as a global ref
    data = pickle.dumps(r1.namespace["sum_squares"])
    restored = pickle.loads(data)

    sandbox.activate(restored)
    r2 = await sandbox.aexec(
        "result = await sum_squares([1, 2, 3, 4])",
        namespace={"sum_squares": restored},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 30


@pytest.mark.asyncio
async def test_sync_calls_async_dep_across_turns():
    """Sync function defined in turn 2 awaits async fn from turn 1."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define async helper
    r1 = await sandbox.aexec("""\
async def fetch(x):
    return x * 10
""")
    assert r1.error is None

    fetch = pickle.loads(pickle.dumps(r1.namespace["fetch"]))

    # Turn 2: define async function that uses the turn-1 async helper
    r2 = await sandbox.aexec("""\
async def process(items):
    results = []
    for item in items:
        results.append(await fetch(item))
    return results
""", namespace={"fetch": fetch})
    assert r2.error is None

    # Turn 3: use composed function
    process = pickle.loads(pickle.dumps(r2.namespace["process"]))
    r3 = await sandbox.aexec(
        "result = await process([1, 2, 3])",
        namespace={"process": process, "fetch": fetch},
    )
    assert r3.error is None
    assert r3.namespace["result"] == [10, 20, 30]


@pytest.mark.asyncio
async def test_async_generator_across_turns():
    """Async generator function pickled and used in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
async def arange(start, stop):
    i = start
    while i < stop:
        yield i
        i += 1
""")
    assert r1.error is None
    assert isinstance(r1.namespace["arange"], StFunction)

    data = pickle.dumps(r1.namespace["arange"])
    restored = pickle.loads(data)

    r2 = await sandbox.aexec("""\
total = 0
async for x in arange(1, 5):
    total += x
""", namespace={"arange": restored})
    assert r2.error is None
    assert r2.namespace["total"] == 10  # 1 + 2 + 3 + 4


@pytest.mark.asyncio
async def test_async_decorator_across_turns():
    """Async decorator pattern: wrapper is async inner function, survives pickle."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
def with_logging(fn):
    async def wrapper(*args):
        result = await fn(*args)
        return ("logged", result)
    return wrapper

async def compute(x):
    return x * x

logged_compute = with_logging(compute)
""")
    assert r1.error is None
    assert isinstance(r1.namespace["logged_compute"], StFunction)

    data = pickle.dumps(r1.namespace["logged_compute"])
    restored = pickle.loads(data)

    r2 = await sandbox.aexec(
        "result = await logged_compute(7)",
        namespace={"logged_compute": restored},
    )
    assert r2.error is None
    assert r2.namespace["result"] == ("logged", 49)


@pytest.mark.asyncio
async def test_async_three_turn_accumulation():
    """Async state accumulates across three turns with pickle boundaries."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    # Turn 1: define async class + populate
    r1 = await sandbox.aexec("""\
class AsyncLog:
    def __init__(self):
        self.entries = []
    async def append(self, msg):
        self.entries.append(msg)
    async def get_all(self):
        return list(self.entries)

log = AsyncLog()
await log.append("turn1")
""")
    assert r1.error is None

    # Turn 2: continue
    ns2 = {
        "AsyncLog": pickle.loads(pickle.dumps(r1.namespace["AsyncLog"])),
        "log": pickle.loads(pickle.dumps(r1.namespace["log"])),
    }
    r2 = await sandbox.aexec('await log.append("turn2")', namespace=ns2)
    assert r2.error is None

    # Turn 3: read
    ns3 = {"log": pickle.loads(pickle.dumps(r2.namespace["log"]))}
    r3 = await sandbox.aexec("result = await log.get_all()", namespace=ns3)
    assert r3.error is None
    assert r3.namespace["result"] == ["turn1", "turn2"]


# --- Regression: getattr gate restored after pickle ---


def test_sbinstance_getattr_gate_restored_after_pickle():
    """Private attrs blocked by policy on StInstance after pickle round-trip."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Obj:
    def __init__(self):
        self.public = 42

o = Obj()
""")
    assert r1.error is None

    # Set a private attr on the real instance from host code
    real = object.__getattribute__(r1.namespace["o"], "_st_instance")
    real._secret = "hidden"

    # Before pickle: gate blocks _secret access in sandbox
    r_check = sandbox.exec("x = o._secret", namespace={
        "Obj": r1.namespace["Obj"],
        "o": r1.namespace["o"],
    })
    assert isinstance(r_check.error, AttributeError)

    # After pickle round-trip: gate must still block _secret
    ns2 = {
        "Obj": pickle.loads(pickle.dumps(r1.namespace["Obj"])),
        "o": pickle.loads(pickle.dumps(r1.namespace["o"])),
    }
    # Public attr works
    r2 = sandbox.exec("x = o.public", namespace=ns2)
    assert r2.error is None
    assert r2.namespace["x"] == 42

    # Private attr still blocked
    r3 = sandbox.exec("x = o._secret", namespace=ns2)
    assert isinstance(r3.error, AttributeError)


# --- Regression: mutual StFunction refs don't cause infinite activation ---


def test_mutual_sbfunction_refs_no_infinite_loop():
    """Mutually referencing StFunctions activate without RecursionError."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def is_even(n):
    if n == 0:
        return True
    return is_odd(n - 1)

def is_odd(n):
    if n == 0:
        return False
    return is_even(n - 1)
""")
    assert r1.error is None

    # Pickle both — frozen globals create circular refs
    data_even = pickle.dumps(r1.namespace["is_even"])
    data_odd = pickle.dumps(r1.namespace["is_odd"])

    is_even = pickle.loads(data_even)
    is_odd = pickle.loads(data_odd)

    # Activate and use — should not hit RecursionError during activation
    r2 = sandbox.exec("""\
result_even = is_even(4)
result_odd = is_odd(3)
""", namespace={"is_even": is_even, "is_odd": is_odd})
    assert r2.error is None
    assert r2.namespace["result_even"] is True
    assert r2.namespace["result_odd"] is True


# --- VFS class pickle ---


def test_vfs_class_pickle_round_trip():
    """StClass imported from VFS survives pickle and works in a later turn."""
    fs = MemoryFS()
    fs.files["/shapes.py"] = b"""\
class Circle:
    def __init__(self, r):
        self.r = r
    def area(self):
        return 3.14 * self.r * self.r
"""

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped", filesystem=fs)

    r1 = sandbox.exec("from shapes import Circle")
    assert r1.error is None
    assert isinstance(r1.namespace["Circle"], StClass)

    data = pickle.dumps(r1.namespace["Circle"])
    restored = pickle.loads(data)

    r2 = sandbox.exec("""\
c = Circle(5)
result = c.area()
""", namespace={"Circle": restored})
    assert r2.error is None
    assert abs(r2.namespace["result"] - 78.5) < 0.1


def test_vfs_class_method_calls_vfs_function():
    """VFS class method calling a VFS helper function, pickled across turns."""
    fs = MemoryFS()
    fs.files["/lib.py"] = b"""\
def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x

class Gauge:
    def __init__(self, lo, hi):
        self.lo = lo
        self.hi = hi
        self.value = lo
    def set(self, x):
        self.value = clamp(x, self.lo, self.hi)
    def read(self):
        return self.value
"""

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped", filesystem=fs)

    r1 = sandbox.exec("from lib import Gauge")
    assert r1.error is None

    # Pickle just the class — clamp is a VFS global, not frozen in class refs
    data = pickle.dumps(r1.namespace["Gauge"])
    restored = pickle.loads(data)

    # Provide clamp alongside Gauge so the class body can find it
    r1b = sandbox.exec("from lib import clamp")
    assert r1b.error is None

    r2 = sandbox.exec("""\
g = Gauge(0, 100)
g.set(150)
result = g.read()
""", namespace={
        "Gauge": restored,
        "clamp": pickle.loads(pickle.dumps(r1b.namespace["clamp"])),
    })
    assert r2.error is None
    assert r2.namespace["result"] == 100


def test_vfs_class_instance_pickle_round_trip():
    """VFS class instance constructed in VFS, pickled and used in later turn."""
    fs = MemoryFS()
    fs.files["/counter.py"] = b"""\
class Counter:
    def __init__(self, start=0):
        self.n = start
    def inc(self):
        self.n += 1
    def value(self):
        return self.n
"""

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped", filesystem=fs)

    r1 = sandbox.exec("""\
from counter import Counter
c = Counter(10)
c.inc()
c.inc()
""")
    assert r1.error is None

    ns2 = {
        "Counter": pickle.loads(pickle.dumps(r1.namespace["Counter"])),
        "c": pickle.loads(pickle.dumps(r1.namespace["c"])),
    }
    r2 = sandbox.exec("""\
c.inc()
result = c.value()
""", namespace=ns2)
    assert r2.error is None
    assert r2.namespace["result"] == 13


# --- Child class relies on frozen Base ref ---


def test_child_class_pickle_without_explicit_base():
    """Pickle only the child class — Base frozen as a ref, auto-activated."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Base:
    def greet(self):
        return "hello"

class Child(Base):
    def farewell(self):
        return "bye"
""")
    assert r1.error is None

    # Pickle only Child — Base should be captured in frozen_refs
    data = pickle.dumps(r1.namespace["Child"])
    restored = pickle.loads(data)

    r2 = sandbox.exec("""\
c = Child()
result = c.greet() + " " + c.farewell()
""", namespace={"Child": restored})
    assert r2.error is None
    assert r2.namespace["result"] == "hello bye"


# --- Closure StFunction + global StFunction both need activation ---


def test_closure_and_global_sbfunction_deps():
    """Function with StFunction in closure AND a global StFunction dep."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def square(x):
    return x * x

def make_fn(helper):
    def f(x):
        return square(helper(x))
    return f

def double(x):
    return x * 2

fn = make_fn(double)
""")
    assert r1.error is None

    # fn has: double in closure, square as global ref
    data = pickle.dumps(r1.namespace["fn"])
    restored = pickle.loads(data)

    # Activate standalone — both frozen closure (double) and frozen globals (square) needed
    sandbox.activate(restored)
    assert restored(3) == 36  # square(double(3)) = square(6) = 36


# --- Namespace override vs frozen closure priority ---


def test_namespace_cannot_override_frozen_closure():
    """Frozen closure wins over namespace for same-named value."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def make_fn(n):
    def f(x):
        return x + n
    return f

add10 = make_fn(10)
""")
    assert r1.error is None

    data = pickle.dumps(r1.namespace["add10"])
    restored = pickle.loads(data)

    # Try to override "n" via namespace — closure should win
    r2 = sandbox.exec("result = add10(5)", namespace={
        "add10": restored,
        "n": 999,
    })
    assert r2.error is None
    assert r2.namespace["result"] == 15  # 5 + 10, not 5 + 999


# --- Deeply nested frozen globals chain ---


def test_deep_frozen_globals_chain():
    """4-level frozen global chain: A -> B -> C -> D, all auto-activated."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def d(x): return x + 1
def c(x): return d(x) * 2
def b(x): return c(x) + 10
def a(x): return b(x) * 3
""")
    assert r1.error is None

    # Pickle only a — d, c, b should be frozen transitively
    data = pickle.dumps(r1.namespace["a"])
    restored = pickle.loads(data)

    sandbox.activate(restored)
    # a(5) = b(5) * 3 = (c(5) + 10) * 3 = (d(5) * 2 + 10) * 3
    #       = (6 * 2 + 10) * 3 = 22 * 3 = 66
    assert restored(5) == 66


# --- find_refs with inactive (pickled) StFunctions ---


def test_find_refs_with_pickled_inactive_namespace():
    """find_refs follows transitive deps through inactive (pickled) StFunctions."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def helper(x): return x + 1
def process(x): return helper(x) * 2
def main(x): return process(x) + 100
""")
    assert r1.error is None

    # Pickle everything — StFunctions are inactive (_compiled=None)
    ns = {k: pickle.loads(pickle.dumps(v))
          for k, v in r1.namespace.items()
          if isinstance(v, StFunction)}

    # find_refs should still discover transitive deps via _global_ref_names
    refs = find_refs("result = main(5)", namespace=ns)
    assert "main" in refs
    assert "process" in refs
    assert "helper" in refs


# --- StInstance with nested StInstance in attrs ---


def test_nested_sbinstance_in_attrs():
    """StInstance whose attrs contain another StInstance, pickled across turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Node:
    def __init__(self, value, child=None):
        self.value = value
        self.child = child
    def depth_values(self):
        result = [self.value]
        if self.child is not None:
            result = result + self.child.depth_values()
        return result

leaf = Node("c")
mid = Node("b", leaf)
root = Node("a", mid)
""")
    assert r1.error is None

    ns2 = {
        "Node": pickle.loads(pickle.dumps(r1.namespace["Node"])),
        "root": pickle.loads(pickle.dumps(r1.namespace["root"])),
    }
    r2 = sandbox.exec("result = root.depth_values()", namespace=ns2)
    assert r2.error is None
    assert r2.namespace["result"] == ["a", "b", "c"]


# --- StFunction stored as instance attribute ---


def test_sbfunction_as_instance_attr():
    """StInstance with an StFunction stored as an attribute, pickled across turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
def double(x):
    return x * 2

class Processor:
    def __init__(self, fn):
        self.fn = fn
    def run(self, x):
        return self.fn(x)

p = Processor(double)
""")
    assert r1.error is None

    ns2 = {
        "Processor": pickle.loads(pickle.dumps(r1.namespace["Processor"])),
        "p": pickle.loads(pickle.dumps(r1.namespace["p"])),
        "double": pickle.loads(pickle.dumps(r1.namespace["double"])),
    }
    r2 = sandbox.exec("result = p.run(21)", namespace=ns2)
    assert r2.error is None
    assert r2.namespace["result"] == 42


# --- StInstance dunder protocols after pickle ---


def test_sbinstance_dunder_protocols_after_pickle():
    """Dunder protocol methods (__len__, __getitem__, etc.) work after pickle."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class MyList:
    def __init__(self):
        self.items = []
    def append(self, x):
        self.items.append(x)
    def __len__(self):
        return len(self.items)
    def __getitem__(self, idx):
        return self.items[idx]
    def __contains__(self, x):
        return x in self.items
    def __iter__(self):
        return iter(self.items)

ml = MyList()
ml.append(10)
ml.append(20)
ml.append(30)
""")
    assert r1.error is None

    ns2 = {
        "MyList": pickle.loads(pickle.dumps(r1.namespace["MyList"])),
        "ml": pickle.loads(pickle.dumps(r1.namespace["ml"])),
    }
    r2 = sandbox.exec("""\
length = len(ml)
first = ml[0]
has_20 = 20 in ml
collected = [x for x in ml]
""", namespace=ns2)
    assert r2.error is None
    assert r2.namespace["length"] == 3
    assert r2.namespace["first"] == 10
    assert r2.namespace["has_20"] is True
    assert r2.namespace["collected"] == [10, 20, 30]


# --- Direct StClass construction after pickle ---


def test_direct_sbclass_construction_after_pickle():
    """StClass constructed via direct call (not sandbox.exec) after pickle."""
    policy = Policy(tick_limit=100)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("""\
class Adder:
    def __init__(self, n):
        self.n = n
    def add(self, x):
        return self.n + x
""")
    assert r1.error is None

    data = pickle.dumps(r1.namespace["Adder"])
    Adder = pickle.loads(data)

    # Activate and call directly from host code
    sandbox.activate(Adder)
    instance = Adder(10)
    assert instance.add(5) == 15


# --- Async: StClass pickled alone, construct + await in later turn ---


@pytest.mark.asyncio
async def test_async_class_pickle_and_construct_later():
    """Async StClass methods work after class-only pickle and later construction."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
class AsyncCalc:
    def __init__(self, base):
        self.base = base
    async def compute(self, x):
        return self.base + x * x
""")
    assert r1.error is None

    data = pickle.dumps(r1.namespace["AsyncCalc"])
    restored = pickle.loads(data)

    r2 = await sandbox.aexec("""\
c = AsyncCalc(100)
result = await c.compute(5)
""", namespace={"AsyncCalc": restored})
    assert r2.error is None
    assert r2.namespace["result"] == 125


# --- Async fn with sync StFunction in closure ---


@pytest.mark.asyncio
async def test_async_fn_with_sync_closure_dep():
    """Async function captures a sync StFunction in closure, pickled across turns."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = await sandbox.aexec("""\
def square(x):
    return x * x

def make_async_mapper(fn):
    async def mapper(items):
        return [fn(x) for x in items]
    return mapper

async_square = make_async_mapper(square)
""")
    assert r1.error is None
    assert isinstance(r1.namespace["async_square"], StFunction)

    data = pickle.dumps(r1.namespace["async_square"])
    restored = pickle.loads(data)

    r2 = await sandbox.aexec(
        "result = await async_square([1, 2, 3, 4])",
        namespace={"async_square": restored},
    )
    assert r2.error is None
    assert r2.namespace["result"] == [1, 4, 9, 16]


# --- Double activation ---


def test_double_activation_no_crash():
    """Activating the same StFunction twice doesn't crash or corrupt state."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="wrapped")

    r1 = sandbox.exec("def add(a, b): return a + b")
    assert r1.error is None

    fn = pickle.loads(pickle.dumps(r1.namespace["add"]))

    # Activate twice
    sandbox.activate(fn)
    assert fn(1, 2) == 3
    sandbox.activate(fn)
    assert fn(3, 4) == 7
