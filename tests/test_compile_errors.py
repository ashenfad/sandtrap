"""Syntax only the compiler rejects comes back as the script's error.

`ast.parse` accepts top-level `await`, and `return` or `yield` at module
level; `compile()` refuses them. That refusal escaped `exec` as a raised
SyntaxError in process, and came back wrapped in a RuntimeError from a
worker, where an ordinary syntax error is `result.error` everywhere
(part of #55).
"""

import pytest

from sandtrap import IsolationUnavailable, Policy, sandbox

ISOLATIONS = ("none", "process", "kernel")


def _exec(isolation, source):
    try:
        sb = sandbox(Policy(timeout=30.0), isolation=isolation)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")
    with sb as active:
        return active.exec(source)


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize(
    "source, line",
    [
        ("x = 1\nawait foo()", 2),
        ("async for a in b:\n    pass", 1),
        ("async with a:\n    pass", 1),
        ("x = 1\ny = [a async for a in b]", 2),
    ],
)
def test_top_level_await_is_the_scripts_error_and_says_why(isolation, source, line):
    result = _exec(isolation, source)
    assert type(result.error) is SyntaxError, repr(result.error)
    assert "top-level await needs an async execution" in str(result.error)
    assert result.error.lineno == line


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("source", ["return 1", "yield 1"])
def test_other_compile_time_errors_are_the_scripts_error(isolation, source):
    result = _exec(isolation, source)
    assert type(result.error) is SyntaxError, repr(result.error)
    assert "await" not in str(result.error)


def test_a_script_that_only_defines_a_coroutine_still_runs():
    """An `async def` at the top is not top-level await."""
    result = _exec("none", "async def f():\n    await g()\nok = True")
    assert result.error is None
    assert result.namespace["ok"] is True
