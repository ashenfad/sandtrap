"""Per-execution stderr capture (``result.stderr``).

Host-side ``sys.stderr`` writes during an exec — registered library
code, ``warnings`` — are captured via a ContextVar-routing proxy
installed over ``sys.stderr``, so concurrent executions in one process
each get their own stream and writes outside any execution fall
through to the real stderr untouched.
"""

import io
import logging
import sys
import threading
import warnings

from sandtrap import Policy, Sandbox
from sandtrap.builtins import _StderrRouter, install_stderr, redirect_stderr


def _sb(policy: Policy | None = None) -> Sandbox:
    return Sandbox(policy or Policy(timeout=10, tick_limit=1_000_000))


# -- host-side writes land in result.stderr -------------------------------


def test_registered_function_stderr_is_captured():
    policy = Policy(timeout=10, tick_limit=1_000_000)

    @policy.fn
    def shout(tag: str) -> None:
        sys.stderr.write(f"host says {tag}\n")

    r = _sb(policy).exec("shout('boo')")
    assert r.error is None
    assert r.stderr == "host says boo\n"
    assert r.stdout == ""


def test_warnings_are_captured():
    policy = Policy(timeout=10, tick_limit=1_000_000)
    policy.module(warnings)

    def stderr_showwarning(message, category, filename, lineno, file=None, line=None):
        # the stdlib default writes to sys.stderr; pytest swaps in a
        # recorder, so pin the default behavior for this test. sys.stderr
        # is looked up at call time — that's the router under test.
        (file or sys.stderr).write(
            warnings.formatwarning(message, category, filename, lineno, line)
        )

    with warnings.catch_warnings():  # restores showwarning + filters
        warnings.simplefilter("always")
        warnings.showwarning = stderr_showwarning
        r = _sb(policy).exec("warnings.warn('careful now')")
    assert r.error is None
    assert "careful now" in r.stderr
    assert "careful now" not in r.stdout


def test_synthetic_sys_stderr_separate_from_stdout():
    r = _sb().exec(
        "import sys\nsys.stderr.write('e1\\n')\nprint('out')",
        stdin="",
    )
    assert r.error is None
    assert r.stderr == "e1\n"
    assert r.stdout == "out\n"


def test_stderr_empty_when_nothing_written():
    r = _sb().exec("x = 1")
    assert r.error is None
    assert r.stderr == ""


def test_stderr_captured_even_when_code_raises():
    policy = Policy(timeout=10, tick_limit=1_000_000)

    @policy.fn
    def moan() -> None:
        sys.stderr.write("pre-crash\n")

    r = _sb(policy).exec("moan()\nraise ValueError('boom')")
    assert r.error is not None
    assert r.stderr == "pre-crash\n"


# -- host prints to a named stream --------------------------------------------


def test_a_host_print_to_a_buffer_writes_there():
    policy = Policy(timeout=10, tick_limit=1_000_000)

    @policy.fn
    def render() -> str:
        buf = io.StringIO()
        print("into the buffer", file=buf)
        return buf.getvalue()

    r = _sb(policy).exec("text = render()")
    assert r.error is None
    assert r.namespace["text"] == "into the buffer\n"
    assert r.stdout == ""


def test_a_host_print_to_stderr_is_captured():
    policy = Policy(timeout=10, tick_limit=1_000_000)

    @policy.fn
    def complain() -> None:
        print("to stderr", file=sys.stderr)

    r = _sb(policy).exec("complain()")
    assert r.error is None
    assert r.stderr == "to stderr\n"
    assert r.stdout == ""


def test_a_host_print_to_stdout_by_name_is_a_print():
    """Naming stdout is the same print as leaving it to the default: it
    reaches the snapshot as well as the captured text."""
    policy = Policy(timeout=10, tick_limit=1_000_000)

    @policy.fn
    def say() -> None:
        print("named", file=sys.stdout)
        print("default")

    r = Sandbox(policy, snapshot_prints=True).exec("say()")
    assert r.error is None
    assert r.stdout == "named\ndefault\n"
    assert r.prints == [("named",), ("default",)]


def test_sandboxed_print_to_a_stream_is_still_refused():
    r = _sb().exec("import sys\nprint('x', file=sys.stderr)", stdin="")
    assert isinstance(r.error, ValueError)
    assert "file=" in str(r.error)


def _log_a_handled_error(logger: logging.Logger) -> str:
    try:
        raise FileNotFoundError("no such file")
    except OSError:
        logger.warning("lookup failed", exc_info=True)
    return "fallback"


def _unhandled_logger(name: str) -> logging.Logger:
    """A logger with no handler anywhere above it, so a record it takes
    goes to logging's last-resort handler, which writes to sys.stderr."""
    logger = logging.getLogger(name)
    logger.propagate = False
    return logger


def log_with_no_handler() -> str:
    return _log_a_handled_error(_unhandled_logger("sandtrap.tests.no_handler"))


def test_a_logged_exception_reaches_its_handler():
    """Formatting a record's traceback prints it into a buffer, which a
    host library calling the logger must be able to do."""
    buf = io.StringIO()
    logger = _unhandled_logger("sandtrap.tests.handled")
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    try:
        policy = Policy(timeout=10, tick_limit=1_000_000)
        policy.fn(lambda: _log_a_handled_error(logger), name="lookup")
        r = _sb(policy).exec("value = lookup()")
    finally:
        logger.removeHandler(handler)
    assert r.error is None
    assert r.namespace["value"] == "fallback"
    assert "lookup failed" in buf.getvalue()
    assert "FileNotFoundError: no such file" in buf.getvalue()


def test_a_logged_exception_with_no_handler_is_captured():
    policy = Policy(timeout=10, tick_limit=1_000_000)
    policy.fn(log_with_no_handler, name="lookup")
    r = _sb(policy).exec("value = lookup()")
    assert r.error is None
    assert r.namespace["value"] == "fallback"
    assert "lookup failed" in r.stderr
    assert "FileNotFoundError: no such file" in r.stderr


# -- the router itself -----------------------------------------------------


def test_router_falls_through_outside_exec():
    original = io.StringIO()
    router = _StderrRouter(original)
    assert router.write("plain\n") == 6
    assert original.getvalue() == "plain\n"

    captured = io.StringIO()
    with redirect_stderr(captured):
        router.write("routed\n")
    router.write("after\n")
    assert captured.getvalue() == "routed\n"
    assert original.getvalue() == "plain\nafter\n"


def test_router_delegates_stream_attrs():
    router = _StderrRouter(sys.__stderr__)
    assert router.encoding == sys.__stderr__.encoding
    assert router.isatty() in (True, False)


def test_install_is_idempotent():
    install_stderr()
    first = sys.stderr
    install_stderr()
    assert sys.stderr is first
    assert isinstance(sys.stderr, _StderrRouter)


def test_no_routing_leaks_after_exec():
    """An exec must never leave stderr routed at a dead capture buffer
    (the contextlib.redirect_stderr failure mode)."""
    from sandtrap.builtins import _sandbox_stderr

    _sb().exec("x = 1")
    assert _sandbox_stderr.get(None) is None


# -- concurrency ------------------------------------------------------------


def test_concurrent_execs_do_not_cross_contaminate():
    def make(tag: str):
        policy = Policy(timeout=10, tick_limit=1_000_000)

        @policy.fn
        def emit() -> None:
            for _ in range(50):
                sys.stderr.write(f"{tag}\n")

        return _sb(policy)

    results: dict[str, object] = {}

    def run(tag: str) -> None:
        results[tag] = make(tag).exec("emit()")

    threads = [threading.Thread(target=run, args=(t,)) for t in ("aaa", "bbb")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    a, b = results["aaa"], results["bbb"]
    assert a.error is None and b.error is None
    assert a.stderr == "aaa\n" * 50
    assert b.stderr == "bbb\n" * 50


# -- process isolation -------------------------------------------------------


def test_process_isolation_stderr_crosses_boundary():
    from sandtrap.process.sandbox import ProcessSandbox

    policy = Policy(timeout=10.0)
    # The real sys module, restricted to stderr: the worker-side router
    # sits at sys.stderr, so the write is captured there and the capture
    # must come back over the wire in ResultMsg.
    policy.module(sys, include="stderr")
    with ProcessSandbox(policy) as ps:
        r = ps.exec("sys.stderr.write('over the wire\\n')")
    assert r.error is None
    assert "over the wire" in r.stderr
    assert "over the wire" not in r.stdout


def test_process_isolation_logged_exception_is_captured():
    from sandtrap.process.sandbox import ProcessSandbox

    policy = Policy(timeout=10.0)
    policy.fn(log_with_no_handler, name="lookup")
    with ProcessSandbox(policy) as ps:
        r = ps.exec("value = lookup()")
    assert r.error is None
    assert r.namespace["value"] == "fallback"
    assert "FileNotFoundError: no such file" in r.stderr
