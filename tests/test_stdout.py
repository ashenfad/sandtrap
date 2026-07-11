"""Per-execution capture of host-side ``sys.stdout`` writes.

Sandboxed ``print`` was always captured (the injected print / global
print patch), but registered library code writing to the REAL
``sys.stdout`` — ``df.info()`` is the canonical case — used to escape
to the host process's terminal, invisible to the caller. A
ContextVar-routing proxy over ``sys.stdout`` (the stderr router's
twin) folds those writes into ``result.stdout`` on the executing
context, in order, while writes outside any execution fall through
untouched. ``passthrough_stdio()`` is the opt-out for host callbacks
that want the real console.
"""

import io
import sys
import threading
import types

from sandtrap import Policy, Sandbox, passthrough_stdio
from sandtrap.builtins import _StdoutRouter, install_stdout, redirect_stdout


def _sb(policy: Policy | None = None) -> Sandbox:
    return Sandbox(policy or Policy(timeout=10, tick_limit=1_000_000))


def _chatty(name: str = "chatty") -> types.ModuleType:
    """A host module whose code writes to the real ``sys.stdout`` at
    call time — the ``df.info()`` shape."""
    mod = types.ModuleType(name)

    def talk(text: str = "host stdout") -> None:
        sys.stdout.write(text + "\n")

    mod.talk = talk
    sys.modules[name] = mod
    return mod


# -- host-side writes land in result.stdout ----------------------------------


def test_registered_module_stdout_is_captured():
    policy = Policy(timeout=10, tick_limit=1_000_000)
    policy.module(_chatty())

    r = _sb(policy).exec("import chatty\nchatty.talk('like df.info')")
    assert r.error is None
    assert r.stdout == "like df.info\n"
    assert r.stderr == ""


def test_prints_and_host_writes_interleave_in_order():
    """Injected print and the stdout router share ONE buffer — output
    order survives, it isn't print-then-host-lump."""
    policy = Policy(timeout=10, tick_limit=1_000_000)
    policy.module(_chatty())

    r = _sb(policy).exec("print('a')\nchatty.talk('mid')\nprint('b')")
    assert r.error is None
    assert r.stdout == "a\nmid\nb\n"


def test_stdout_captured_even_when_code_raises():
    policy = Policy(timeout=10, tick_limit=1_000_000)
    policy.module(_chatty())

    r = _sb(policy).exec("chatty.talk('pre-crash')\nraise ValueError('boom')")
    assert r.error is not None
    assert r.stdout == "pre-crash\n"


def test_host_thread_writes_are_captured():
    """Capture routing follows host libraries into threads they spawn
    (the contextvar-propagating threading patches install with stdio
    capture, not just with network gating)."""
    policy = Policy(timeout=10, tick_limit=1_000_000, allow_network=True)

    @policy.fn
    def fan_out() -> None:
        def work() -> None:
            sys.stdout.write("from a thread\n")

        t = threading.Thread(target=work)
        t.start()
        t.join()

    r = _sb(policy).exec("fan_out()")
    assert r.error is None
    assert r.stdout == "from a thread\n"


# -- passthrough: host callbacks reach the real console ----------------------


def test_passthrough_stdio_reaches_the_real_console(capsys):
    policy = Policy(timeout=10, tick_limit=1_000_000)

    @policy.fn
    def console(tag: str) -> None:
        with passthrough_stdio():
            print(f"console {tag}")
        print(f"captured {tag}")

    r = _sb(policy).exec("console('x')")
    assert r.error is None
    assert r.stdout == "captured x\n"
    assert "console x" in capsys.readouterr().out


# -- the router itself --------------------------------------------------------


def test_router_falls_through_outside_exec():
    original = io.StringIO()
    router = _StdoutRouter(original)
    assert router.write("plain\n") == 6
    assert original.getvalue() == "plain\n"

    captured = io.StringIO()
    with redirect_stdout(captured):
        router.write("routed\n")
    router.write("after\n")
    assert captured.getvalue() == "routed\n"
    assert original.getvalue() == "plain\nafter\n"


def test_router_delegates_stream_attrs():
    router = _StdoutRouter(sys.__stdout__)
    assert router.encoding == sys.__stdout__.encoding
    assert router.isatty() in (True, False)


def test_install_is_idempotent():
    install_stdout()
    first = sys.stdout
    install_stdout()
    assert sys.stdout is first
    assert isinstance(sys.stdout, _StdoutRouter)


def test_no_routing_leaks_after_exec():
    from sandtrap.builtins import _sandbox_stdout

    _sb().exec("x = 1")
    assert _sandbox_stdout.get(None) is None


# -- concurrency --------------------------------------------------------------


def test_concurrent_execs_do_not_cross_contaminate():
    def make(tag: str) -> Sandbox:
        policy = Policy(timeout=10, tick_limit=1_000_000)

        @policy.fn
        def emit() -> None:
            for _ in range(50):
                sys.stdout.write(f"{tag}\n")

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
    assert a.stdout == "aaa\n" * 50
    assert b.stdout == "bbb\n" * 50


# -- process isolation ---------------------------------------------------------


def test_process_isolation_stdout_crosses_boundary():
    from sandtrap.process.sandbox import ProcessSandbox

    policy = Policy(timeout=10.0)
    policy.module(_chatty("chatty_proc"))
    with ProcessSandbox(policy) as ps:
        r = ps.exec("print('sandbox print')\nchatty_proc.talk('over the wire')")
    assert r.error is None
    assert r.stdout == "sandbox print\nover the wire\n"
