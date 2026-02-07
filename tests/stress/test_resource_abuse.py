"""Stress tests: resource abuse that should be caught by limits."""

from sblite import Policy, Sandbox
from sblite.errors import SbTickLimit, SbTimeout

# --- Infinite loops ---


def test_while_true():
    sandbox = Sandbox(Policy(timeout=0.2))
    result = sandbox.exec("while True: pass")
    assert isinstance(result.error, SbTimeout)


def test_infinite_recursion():
    sandbox = Sandbox(Policy(timeout=0.2))
    result = sandbox.exec("""\
def f(): f()
f()
""")
    assert result.error is not None


def test_infinite_generator():
    sandbox = Sandbox(Policy(tick_limit=500))
    result = sandbox.exec("""\
def gen():
    while True:
        yield 1
for x in gen():
    pass
""")
    assert isinstance(result.error, SbTickLimit)


def test_infinite_comprehension():
    sandbox = Sandbox(Policy(tick_limit=500))
    result = sandbox.exec("""\
def gen():
    i = 0
    while True:
        yield i
        i += 1
x = [i for i in gen()]
""")
    assert isinstance(result.error, SbTickLimit)


# --- Exponential growth ---


def test_string_growth_bomb():
    """Repeated string doubling should hit memory or tick limit."""
    sandbox = Sandbox(Policy(tick_limit=5000, timeout=2.0, memory_limit=100))
    result = sandbox.exec("""\
x = 'a'
for i in range(50):
    x = x + x
""")
    assert result.error is not None


def test_list_growth_bomb():
    sandbox = Sandbox(Policy(tick_limit=5000, timeout=2.0, memory_limit=100))
    result = sandbox.exec("""\
x = [0]
for i in range(50):
    x = x + x
""")
    assert result.error is not None


def test_nested_list_bomb():
    """Exponential nesting via repeated wrapping."""
    sandbox = Sandbox(Policy(tick_limit=5000, timeout=2.0, memory_limit=100))
    result = sandbox.exec("""\
x = [1]
for i in range(100):
    x = [x, x]
""")
    # Should hit either memory limit or tick limit — not hang
    assert result.error is not None or result.ticks <= 5000


# --- Recursive bombs ---


def test_recursive_fib_hits_tick_limit():
    sandbox = Sandbox(Policy(tick_limit=500))
    result = sandbox.exec("""\
def fib(n):
    if n <= 1:
        return n
    return fib(n-1) + fib(n-2)
fib(100)
""")
    assert isinstance(result.error, SbTickLimit)


def test_mutual_recursion_hits_limit():
    sandbox = Sandbox(Policy(tick_limit=500))
    result = sandbox.exec("""\
def f(n):
    return g(n)
def g(n):
    return f(n)
f(0)
""")
    assert isinstance(result.error, (SbTickLimit, RecursionError))


# --- CPU-intensive loops ---


def test_busy_loop_with_tick_limit():
    sandbox = Sandbox(Policy(tick_limit=100))
    result = sandbox.exec("""\
total = 0
for i in range(10000):
    for j in range(10000):
        total += 1
""")
    assert isinstance(result.error, SbTickLimit)


def test_deeply_nested_loops():
    sandbox = Sandbox(Policy(tick_limit=1000))
    result = sandbox.exec("""\
for a in range(100):
    for b in range(100):
        for c in range(100):
            pass
""")
    assert isinstance(result.error, SbTickLimit)
