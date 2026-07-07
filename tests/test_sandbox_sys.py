"""Synthetic safe `sys` (stdin/stdout/stderr/argv) + stdin-backed input().

Passing stdin/argv to exec exposes a minimal sys — real idioms work,
interpreter internals stay unreachable, and behavior is unchanged when
the params aren't given.
"""

import io

import pytest

from sandtrap import Policy, Sandbox, VirtualFS


def _sb() -> Sandbox:
    return Sandbox(Policy(timeout=10, tick_limit=1_000_000))


# -- stdin ---------------------------------------------------------------


def test_read_stdin_via_import():
    r = _sb().exec(
        "import sys\ntotal = sum(int(x) for x in sys.stdin)\nprint(total)",
        stdin="10\n20\n30\n",
    )
    assert r.error is None
    assert r.stdout == "60\n"


def test_stdin_accepts_a_stream():
    r = _sb().exec(
        "import sys\nprint(sys.stdin.read().upper())",
        stdin=io.StringIO("hi there"),
    )
    assert r.error is None
    assert r.stdout == "HI THERE\n"


def test_bare_sys_without_import():
    # sys is a bare name too (like registered modules), no import needed
    r = _sb().exec("print(sys.stdin.read())", stdin="abc")
    assert r.error is None
    assert r.stdout == "abc\n"


def test_from_sys_import():
    r = _sb().exec(
        "from sys import stdin, argv\nprint(argv[0], stdin.read())",
        stdin="data",
        argv=["prog"],
    )
    assert r.error is None
    assert r.stdout == "prog data\n"


def test_from_sys_import_unknown_name():
    r = _sb().exec("from sys import modules", stdin="")
    assert r.error is not None
    assert isinstance(r.error, ImportError)


# -- argv ----------------------------------------------------------------


def test_argv():
    r = _sb().exec("import sys\nprint(sys.argv)", argv=["script.py", "a", "b"])
    assert r.error is None
    assert r.stdout == "['script.py', 'a', 'b']\n"


def test_argv_defaults_when_only_stdin_given():
    r = _sb().exec("import sys\nprint(sys.argv)", stdin="")
    assert r.error is None
    assert r.stdout == "['']\n"


# -- stdout / stderr -----------------------------------------------------


def test_stdout_write_is_captured():
    r = _sb().exec("import sys\nsys.stdout.write('x\\n')\nprint('y')", stdin="")
    assert r.error is None
    assert r.stdout == "x\ny\n"


def test_stderr_write_is_captured():
    r = _sb().exec("import sys\nsys.stderr.write('err\\n')", stdin="")
    assert r.error is None
    assert "err" in r.stdout  # captured alongside stdout in-sandbox


def test_stdout_has_file_like_attrs():
    # libraries (logging, rich) inspect these on sys.stdout
    r = _sb().exec(
        "import sys\nprint(sys.stdout.encoding, sys.stdout.isatty(), sys.stdout.closed)",
        stdin="",
    )
    assert r.error is None
    assert r.stdout == "utf-8 False False\n"


# -- input() -------------------------------------------------------------


def test_input_reads_line_and_writes_prompt():
    r = _sb().exec("n = input('name? ')\nprint('hi', n)", stdin="ada\nrest\n")
    assert r.error is None
    assert r.stdout == "name? hi ada\n"


def test_input_eof_raises():
    r = _sb().exec("input()", stdin="")
    assert r.error is not None
    assert isinstance(r.error, EOFError)


def test_input_strips_crlf():
    r = _sb().exec("print(repr(input()))", stdin="line\r\nrest")
    assert r.error is None
    assert r.stdout == "'line'\n"  # no trailing \r


def test_input_available_in_vfs_helper():
    fs = VirtualFS({})
    fs.write("/helpers.py", b"def prompt():\n    return input()\n")
    sb = Sandbox(Policy(timeout=10, tick_limit=1_000_000), filesystem=fs)
    r = sb.exec("from helpers import prompt\nprint(prompt())", stdin="from-helper\n")
    assert r.error is None
    assert r.stdout == "from-helper\n"


# -- backward compatibility ----------------------------------------------


def test_sys_blocked_without_params():
    r = _sb().exec("import sys")
    assert r.error is not None
    assert "sys" in str(r.error)


def test_input_unavailable_without_params():
    r = _sb().exec("input()")
    assert r.error is not None  # NameError: input not defined


# -- safety --------------------------------------------------------------


@pytest.mark.parametrize("attr", ["modules", "settrace", "exit", "path", "_getframe"])
def test_dangerous_sys_attrs_unreachable(attr):
    r = _sb().exec(f"import sys\nsys.{attr}", stdin="")
    assert r.error is not None
    assert isinstance(r.error, AttributeError)


def test_sys_not_leaked_into_result_namespace():
    r = _sb().exec("import sys\nx = 1", stdin="")
    assert r.error is None
    assert "sys" not in r.namespace
    assert r.namespace.get("x") == 1


# -- async path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_aexec_stdin():
    r = await _sb().aexec("import sys\nprint(sys.stdin.read())", stdin="async-in")
    assert r.error is None
    assert r.stdout == "async-in\n"
