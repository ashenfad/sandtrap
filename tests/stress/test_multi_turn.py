"""Stress tests: multi-turn workflows.

Sandbox-defined functions and classes are plain Python objects. They do
not pickle, so a turn hands them to the next one by putting them back in
``namespace`` — the same dict the previous turn's ``ExecResult`` carried
them out in. These tests drive that loop: define in one turn, use in the
next, several turns deep, across errors and redefinitions.
"""

import pytest

from sandtrap import Policy, Sandbox, VirtualFS
from sandtrap.errors import StTickLimit

# --- Function reuse ---


def test_function_reused_in_a_later_turn():
    """Define fn in turn 1, call it in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("def double(x): return x * 2")
    assert r1.error is None

    r2 = sandbox.exec(
        "result = double(21)", namespace={"double": r1.namespace["double"]}
    )
    assert r2.error is None
    assert r2.namespace["result"] == 42


def test_closure_survives_into_a_later_turn():
    """A closure variable captured in turn 1 is still bound in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("""\
def make_adder(n):
    def add(x):
        return x + n
    return add
add10 = make_adder(10)
""")
    assert r1.error is None

    r2 = sandbox.exec("result = add10(5)", namespace={"add10": r1.namespace["add10"]})
    assert r2.error is None
    assert r2.namespace["result"] == 15


def test_multiple_functions_across_turns():
    """Several functions defined in turn 1, all used in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("""\
def add(a, b): return a + b
def mul(a, b): return a * b
def sub(a, b): return a - b
""")
    assert r1.error is None

    ns = {name: r1.namespace[name] for name in ("add", "mul", "sub")}
    r2 = sandbox.exec("result = add(mul(3, 4), sub(10, 5))", namespace=ns)
    assert r2.error is None
    assert r2.namespace["result"] == 17


def test_function_defined_in_turn2_uses_turn1_fn():
    """Turn 2 defines a new function that calls a turn 1 function."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("def square(x): return x * x")
    assert r1.error is None

    r2 = sandbox.exec(
        """\
def sum_of_squares(a, b):
    return square(a) + square(b)
result = sum_of_squares(3, 4)
""",
        namespace={"square": r1.namespace["square"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 25


# --- Class reuse ---


def test_class_constructed_in_a_later_turn():
    """Define a class in turn 1, construct it in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

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

    r2 = sandbox.exec(
        """\
c = Counter(10)
c.inc()
c.inc()
result = c.value()
""",
        namespace={"Counter": r1.namespace["Counter"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 12


def test_instance_keeps_its_state_across_turns():
    """An instance built in turn 1 carries its attributes into turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("""\
class Bag:
    def __init__(self):
        self.items = []
    def add(self, x):
        self.items.append(x)

bag = Bag()
bag.add("first")
""")
    assert r1.error is None

    r2 = sandbox.exec(
        'bag.add("second")\nresult = list(bag.items)',
        namespace={"bag": r1.namespace["bag"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == ["first", "second"]


def test_class_with_inheritance_across_turns():
    """A subclass defined in turn 2 over a base class from turn 1."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("""\
class Base:
    def greet(self):
        return "hello"
""")
    assert r1.error is None

    r2 = sandbox.exec(
        """\
class Child(Base):
    def farewell(self):
        return "bye"

c = Child()
result = c.greet() + " " + c.farewell()
""",
        namespace={"Base": r1.namespace["Base"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == "hello bye"


# --- Three-turn chains ---


def test_three_turn_accumulation():
    """State accumulates across three turns."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

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

    r2 = sandbox.exec(
        's.add("turn2")',
        namespace={"State": r1.namespace["State"], "s": r1.namespace["s"]},
    )
    assert r2.error is None

    r3 = sandbox.exec("result = s.get()", namespace={"s": r2.namespace["s"]})
    assert r3.error is None
    assert r3.namespace["result"] == ["turn1", "turn2"]


def test_function_evolution_across_turns():
    """Turn 1 defines a helper, turn 2 composes over it, turn 3 calls it."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("def square(x): return x * x")
    assert r1.error is None

    r2 = sandbox.exec(
        """\
def sum_squares(lst):
    return sum(square(x) for x in lst)
""",
        namespace={"square": r1.namespace["square"]},
    )
    assert r2.error is None

    # ``square`` is a global of ``sum_squares``, not a closure variable, so
    # turn 3 has to supply it alongside the composed function.
    r3 = sandbox.exec(
        "result = sum_squares([1, 2, 3, 4])",
        namespace={
            "sum_squares": r2.namespace["sum_squares"],
            "square": r1.namespace["square"],
        },
    )
    assert r3.error is None
    assert r3.namespace["result"] == 30


# --- Direct calls between turns ---


def test_direct_call_between_turns_keeps_the_tick_limit():
    """A function pulled out of a result still checkpoints when called."""
    sandbox = Sandbox(Policy(tick_limit=100))

    r1 = sandbox.exec("""\
def spin():
    while True:
        pass
""")
    assert r1.error is None

    with pytest.raises(StTickLimit):
        r1.namespace["spin"]()


# --- VFS module across turns ---


def test_vfs_module_function_reused_across_turns():
    """A function imported from a VFS module works in the next turn."""
    fs = VirtualFS({})
    fs.write("/helpers.py", b"def triple(x): return x * 3")
    sandbox = Sandbox(Policy(tick_limit=10_000), filesystem=fs)

    r1 = sandbox.exec("from helpers import triple\nresult = triple(3)")
    assert r1.error is None
    assert r1.namespace["result"] == 9

    r2 = sandbox.exec(
        "result = triple(4)", namespace={"triple": r1.namespace["triple"]}
    )
    assert r2.error is None
    assert r2.namespace["result"] == 12


def test_vfs_module_edited_between_turns():
    """Re-importing after a VFS edit picks up the new body."""
    fs = VirtualFS({})
    fs.write("/helpers.py", b"def scale(x): return x * 2")
    sandbox = Sandbox(Policy(tick_limit=10_000), filesystem=fs)

    r1 = sandbox.exec("from helpers import scale\nresult = scale(5)")
    assert r1.error is None
    assert r1.namespace["result"] == 10

    fs.write("/helpers.py", b"def scale(x): return x * 10")
    r2 = sandbox.exec("from helpers import scale\nresult = scale(5)")
    assert r2.error is None
    assert r2.namespace["result"] == 50


# --- Error recovery across turns ---


def test_error_in_turn_does_not_corrupt_prior_state():
    """Turn 2 errors, but turn 1 state is still usable in turn 3."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("def double(x): return x * 2")
    assert r1.error is None
    double = r1.namespace["double"]

    r2 = sandbox.exec("result = double(1) + oops", namespace={"double": double})
    assert r2.error is not None

    r3 = sandbox.exec("result = double(21)", namespace={"double": double})
    assert r3.error is None
    assert r3.namespace["result"] == 42


# --- Function redefinition across turns ---


def test_redefine_function_across_turns():
    """Turn 1 defines f, turn 2 redefines f, turn 3 uses the new one."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("def transform(x): return x * 2")
    assert r1.error is None

    r2 = sandbox.exec("def transform(x): return x * x")
    assert r2.error is None

    r3 = sandbox.exec(
        "result = transform(5)", namespace={"transform": r2.namespace["transform"]}
    )
    assert r3.error is None
    assert r3.namespace["result"] == 25  # x*x, not x*2

    # The earlier definition is a separate object and still behaves as it did.
    r4 = sandbox.exec(
        "result = transform(5)", namespace={"transform": r1.namespace["transform"]}
    )
    assert r4.error is None
    assert r4.namespace["result"] == 10


# --- Higher-order functions across turns ---


def test_higher_order_function_result_across_turns():
    """A function returned by a factory in turn 1 is callable in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("""\
def make_multiplier(k):
    def multiply(x):
        return x * k
    return multiply

times3 = make_multiplier(3)
""")
    assert r1.error is None

    r2 = sandbox.exec(
        "result = times3(14)", namespace={"times3": r1.namespace["times3"]}
    )
    assert r2.error is None
    assert r2.namespace["result"] == 42


def test_decorator_applied_across_turns():
    """A decorator from turn 1 decorates a function defined in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = sandbox.exec("""\
def twice(fn):
    def wrapper(x):
        return fn(fn(x))
    return wrapper
""")
    assert r1.error is None

    r2 = sandbox.exec(
        """\
@twice
def inc(x):
    return x + 1

result = inc(5)
""",
        namespace={"twice": r1.namespace["twice"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 7


# --- Async multi-turn ---


@pytest.mark.asyncio
async def test_async_function_across_turns():
    """An async function defined in turn 1 is awaited in turn 2."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = await sandbox.aexec("""\
async def double(x):
    return x * 2
""")
    assert r1.error is None

    r2 = await sandbox.aexec(
        "result = await double(21)",
        namespace={"double": r1.namespace["double"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 42


@pytest.mark.asyncio
async def test_async_closure_across_turns():
    """An inner async def carries its closure into the next turn."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = await sandbox.aexec("""\
def make_scaler(k):
    async def scale(x):
        return x * k
    return scale

times5 = make_scaler(5)
""")
    assert r1.error is None

    r2 = await sandbox.aexec(
        "result = await times5(8)",
        namespace={"times5": r1.namespace["times5"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 40


@pytest.mark.asyncio
async def test_async_class_method_across_turns():
    """An async method on a turn 1 class is awaited on a turn 2 instance."""
    sandbox = Sandbox(Policy(tick_limit=10_000))

    r1 = await sandbox.aexec("""\
class Service:
    def __init__(self, factor):
        self.factor = factor
    async def run(self, x):
        return x * self.factor
""")
    assert r1.error is None

    r2 = await sandbox.aexec(
        """\
svc = Service(6)
result = await svc.run(7)
""",
        namespace={"Service": r1.namespace["Service"]},
    )
    assert r2.error is None
    assert r2.namespace["result"] == 42
