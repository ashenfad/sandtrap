"""Stress tests: multi-turn workflows with pickle round-trips."""

import pickle

import pytest

from sblite import MemoryFS, Policy, Sandbox
from sblite.wrappers import SbFunction
from sblite.errors import SbTickLimit

# --- Function pickle + reuse ---


def test_function_pickle_and_call_in_second_turn():
    """Define fn in turn 1, pickle, use in turn 2."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    with pytest.raises(SbTickLimit):
        spin()


# --- Self-contained pickle (frozen globals) ---


def test_function_self_contained_after_pickle():
    """Pickled function works standalone via frozen globals."""
    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="task")

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
    sandbox = Sandbox(policy, mode="task")

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
    """VFS module function (SbFunction in task mode) survives pickle."""
    fs = MemoryFS()
    fs.files["/mathlib.py"] = "def double(x): return x * 2"

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="task", filesystem=fs)

    # Turn 1: import from VFS and define function that uses it
    r1 = sandbox.exec("""\
from mathlib import double
def quadruple(x):
    return double(double(x))
""")
    assert r1.error is None
    assert isinstance(r1.namespace["double"], SbFunction)

    # Pickle only quadruple
    data = pickle.dumps(r1.namespace["quadruple"])
    restored = pickle.loads(data)

    # Activate standalone — frozen double (SbFunction from VFS) makes it work
    sandbox.activate(restored)
    assert restored(5) == 20


def test_vfs_module_function_in_multi_turn():
    """VFS module dep available via frozen globals across turns."""
    fs = MemoryFS()
    fs.files["/utils.py"] = """\
def add(a, b): return a + b
def mul(a, b): return a * b
"""

    policy = Policy(tick_limit=10_000)
    sandbox = Sandbox(policy, mode="task", filesystem=fs)

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
               if isinstance(v, SbFunction)}

    # Turn 2: restore only dot and use it
    dot = pickle.loads(ns_data["dot"])
    r2 = sandbox.exec(
        "result = dot([1, 2, 3], [4, 5, 6])",
        namespace={"dot": dot},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 32  # 1*4 + 2*5 + 3*6
