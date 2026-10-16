"""A write lost when its file is closed implicitly fails the execution.

``open(p, "w").write(x)`` saves the file from a finalizer when the last
reference goes. If that save fails, the execution that dropped the file
fails: with ``StLostWrite`` naming the file when it had no error of its
own, and with a note on its error when it had one.
"""

import asyncio
import errno
import gc
import pickle
import sys
import threading

import monkeyfs
import pytest

from sandtrap import IsolationUnavailable, Policy, StError, StLostWrite, sandbox
from sandtrap.errors import lost_write_error
from sandtrap.fs import VirtualFS

ISOLATIONS = ("none", "process", "kernel")
RUNNERS = ("exec", "aexec")

#: In-process files are monkeyfs's; a release without ``LostWriteError``
#: discards a failed save, so there is nothing for sandtrap to see.
IN_PROCESS_REPORTS = hasattr(monkeyfs, "LostWriteError")
NO_IN_PROCESS_REPORT = (
    "the installed monkeyfs has no LostWriteError, so a lost in-process "
    "write is discarded before sandtrap can see it"
)

QUOTA = 1024


class QuotaFS(VirtualFS):
    """Refuses any write over 1 KiB, as a size quota would."""

    def write(self, path, data, *args, **kwargs):
        if len(data) > QUOTA:
            raise OSError(errno.ENOSPC, "quota exceeded", path)
        return super().write(path, data, *args, **kwargs)


def _sandbox(isolation, fs, policy=None):
    if isolation == "none" and not IN_PROCESS_REPORTS:
        pytest.skip(NO_IN_PROCESS_REPORT)
    try:
        return sandbox(
            policy or Policy(timeout=30.0), isolation=isolation, filesystem=fs
        )
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


def _run(sb, runner, source, **kwargs):
    if runner == "aexec":
        return asyncio.run(sb.aexec(source, **kwargs))
    return sb.exec(source, **kwargs)


@pytest.fixture
def unraisable(monkeypatch):
    """What reaches the hook sandtrap's own hook passes things on to."""
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: seen.append(u.exc_value))
    return seen


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("mode", ["w", "wb"])
def test_lost_write_fails_a_successful_execution(isolation, runner, mode):
    fs = QuotaFS({})
    data = "'x' * 4096" if mode == "w" else "b'x' * 4096"
    with _sandbox(isolation, fs) as sb:
        result = _run(
            sb, runner, f"open('/f.txt', {mode!r}).write({data})\nprint('after')"
        )

    error = result.error
    assert isinstance(error, StLostWrite)
    assert isinstance(error, OSError)
    assert isinstance(error, StError)
    assert error.filename == "/f.txt"
    assert error.errno == errno.ENOSPC
    assert "quota exceeded" in str(error)
    assert isinstance(error.__cause__, OSError)
    assert error.__cause__.strerror == "quota exceeded"
    # The code went on past the write, as it does with a real file.
    assert result.stdout == "after\n"
    assert not fs.exists("/f.txt")


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_lost_write_is_noted_on_a_failed_execution(isolation, runner):
    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = _run(
            sb,
            runner,
            "open('/f.txt', 'w').write('x' * 4096)\n"
            "open('/g.txt', 'w').write('y' * 4096)\n"
            "1 / 0",
        )

    assert isinstance(result.error, ZeroDivisionError)
    notes = result.error.__notes__
    assert len(notes) == 2
    assert "'/f.txt'" in notes[0] and "quota exceeded" in notes[0]
    assert "'/g.txt'" in notes[1]
    if sys.version_info < (3, 11):
        # 3.10's tracebacks don't show __notes__, so the note is printed too.
        assert notes[0] in result.stderr
        assert notes[1] in result.stderr


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_later_lost_writes_are_noted_on_the_first(isolation, runner):
    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = _run(
            sb,
            runner,
            "open('/f.txt', 'w').write('x' * 4096)\n"
            "open('/g.txt', 'w').write('y' * 4096)\n",
        )

    assert isinstance(result.error, StLostWrite)
    assert result.error.filename == "/f.txt"
    assert len(result.error.__notes__) == 1
    assert "'/g.txt'" in result.error.__notes__[0]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_explicit_close_still_raises_into_the_code(isolation, runner, unraisable):
    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = _run(
            sb,
            runner,
            "try:\n"
            "    with open('/f.txt', 'w') as f:\n"
            "        f.write('x' * 4096)\n"
            "except OSError as e:\n"
            "    caught = e.strerror\n",
        )
        assert result.error is None
        assert result.namespace["caught"] == "quota exceeded"

        result = _run(
            sb, runner, "with open('/g.txt', 'w') as f:\n    f.write('x' * 4096)\n"
        )
        assert type(result.error) is OSError
        assert not hasattr(result.error, "__notes__")

    # The handle the failed close left open is finalized later, and has
    # nothing new to report.
    del result
    gc.collect()
    assert unraisable == []


@pytest.mark.parametrize(
    "change",
    ["f.write('y' * 10)", "f.writelines(['y', 'z'])", "f.truncate(2048)"],
    ids=["write", "writelines", "truncate"],
)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_change_after_a_failed_close_is_lost_if_dropped(isolation, change):
    """A failed close() reported the content it held; content changed
    after it was never reported, so losing it fails the execution."""
    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = sb.exec(
            "f = open('/f.txt', 'w')\n"
            "f.write('x' * 4096)\n"
            "try:\n"
            "    f.close()\n"
            "except OSError:\n"
            "    pass\n"
            f"{change}\n"
            "del f\n"
        )
    assert isinstance(result.error, StLostWrite), result.error
    assert result.error.filename == "/f.txt"


@pytest.mark.parametrize("isolation", ("process", "kernel"))
def test_a_failed_flush_is_not_reported_again(isolation):
    """flush() pushes in a worker; a failure it raised was the code's to
    handle, and the same content lost at finalization is not charged
    again."""
    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = sb.exec(
            "f = open('/f.txt', 'w')\n"
            "f.write('x' * 4096)\n"
            "try:\n"
            "    f.flush()\n"
            "except OSError:\n"
            "    print('flush refused')\n"
            "del f\n"
        )
    assert result.error is None, result.error
    assert result.stdout == "flush refused\n"


@pytest.mark.parametrize("isolation", ("process", "kernel"))
def test_writelines_and_truncate_need_a_writable_file(isolation):
    fs = QuotaFS({})
    fs.write("/r.txt", b"abc")
    with _sandbox(isolation, fs) as sb:
        lines = sb.exec("open('/r.txt').writelines(['x'])\n")
        cut = sb.exec("open('/r.txt').truncate(1)\n")
    for result in (lines, cut):
        assert type(result.error).__name__ == "UnsupportedOperation", result.error
    assert fs.read("/r.txt") == b"abc"


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_successful_implicit_close_is_unchanged(isolation, runner):
    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = _run(sb, runner, "open('/f.txt', 'w').write('small')\nprint('ok')")

    assert result.error is None
    assert result.stdout == "ok\n"
    assert fs.read("/f.txt") == b"small"


@pytest.mark.skipif(not IN_PROCESS_REPORTS, reason=NO_IN_PROCESS_REPORT)
def test_file_finalized_after_the_execution_goes_to_the_previous_hook(unraisable):
    fs = QuotaFS({})
    with sandbox(Policy(timeout=30.0), filesystem=fs) as sb:
        result = sb.exec("f = open('/f.txt', 'w')\nf.write('x' * 4096)")
        assert result.error is None
        del result
        gc.collect()

    [error] = unraisable
    assert isinstance(error, monkeyfs.LostWriteError)
    assert error.filename == "/f.txt"


def test_other_unraisable_exceptions_pass_through(unraisable):
    class Noisy:
        def __del__(self):
            raise RuntimeError("not a lost write")

    fs = QuotaFS({})
    with sandbox(Policy(timeout=30.0), filesystem=fs) as sb:
        result = sb.exec("make()", namespace={"make": Noisy})

    assert result.error is None
    [error] = unraisable
    assert isinstance(error, RuntimeError)


@pytest.mark.skipif(not IN_PROCESS_REPORTS, reason=NO_IN_PROCESS_REPORT)
def test_concurrent_threads_keep_their_own_lost_writes():
    # Both executions are inside exec when the first one loses its write.
    barrier = threading.Barrier(2, timeout=10)
    results = {}

    def run(name, source):
        fs = QuotaFS({})
        with sandbox(Policy(timeout=30.0), filesystem=fs) as sb:
            results[name] = sb.exec(source, namespace={"meet": barrier.wait})

    losing = threading.Thread(
        target=run,
        args=("losing", "meet()\nopen('/a.txt', 'w').write('x' * 4096)\nmeet()"),
    )
    clean = threading.Thread(
        target=run,
        args=("clean", "meet()\nopen('/b.txt', 'w').write('ok')\nmeet()"),
    )
    losing.start()
    clean.start()
    losing.join()
    clean.join()

    assert isinstance(results["losing"].error, StLostWrite)
    assert results["losing"].error.filename == "/a.txt"
    assert results["clean"].error is None


@pytest.mark.skipif(not IN_PROCESS_REPORTS, reason=NO_IN_PROCESS_REPORT)
def test_concurrent_tasks_keep_their_own_lost_writes():
    async def main():
        both_in = asyncio.Event()
        lost = asyncio.Event()
        ready = []

        async def arrive():
            ready.append(1)
            if len(ready) == 2:
                both_in.set()
            await both_in.wait()

        async def run(source):
            fs = QuotaFS({})
            sb = sandbox(Policy(timeout=30.0), filesystem=fs)
            return await sb.aexec(
                source,
                namespace={
                    "arrive": arrive,
                    "lost": lost.set,
                    "after_loss": lost.wait,
                },
            )

        return await asyncio.gather(
            run("await arrive()\nopen('/a.txt', 'w').write('x' * 4096)\nlost()\n"),
            run(
                "await arrive()\nawait after_loss()\nopen('/b.txt', 'w').write('ok')\n"
            ),
        )

    losing, clean = asyncio.run(main())

    assert isinstance(losing.error, StLostWrite)
    assert clean.error is None


def test_lost_write_error_pickles_with_its_cause():
    cause = OSError(errno.ENOSPC, "quota exceeded", "/f.txt")
    error = lost_write_error("/f.txt", cause)
    error.add_note("noted") if hasattr(error, "add_note") else None

    copy = pickle.loads(pickle.dumps(error))

    assert type(copy) is StLostWrite
    assert copy.filename == "/f.txt"
    assert copy.errno == errno.ENOSPC
    assert str(copy) == str(error)
    assert isinstance(copy.__cause__, OSError)
    assert copy.__cause__.strerror == "quota exceeded"
    if hasattr(error, "add_note"):
        assert copy.__notes__ == ["noted"]


def test_cause_without_errno_reports_eio():
    error = lost_write_error("/f.txt", RuntimeError("backend down"))

    assert error.errno == errno.EIO
    assert "RuntimeError: backend down" in str(error)


@pytest.mark.parametrize("isolation", ("process", "kernel"))
def test_an_open_file_does_not_hold_up_shutdown(isolation):
    """A file still open when the sandbox closes is finalized as the worker
    exits, after the parent has stopped answering host calls: its push is
    refused at once instead of waiting to be killed."""
    import time

    fs = QuotaFS({})
    with _sandbox(isolation, fs) as sb:
        result = sb.exec("f = open('/f.txt', 'w')\nf.write('abc')\n")
        assert result.error is None, result.error
        started = time.monotonic()
    assert time.monotonic() - started < 2.0
