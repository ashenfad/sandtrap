"""Captured output past ``max_stdout``: the tail, or both ends.

``Policy(stdout_keep="tail")`` (the default) keeps the most recent
``max_stdout`` characters; ``"ends"`` keeps the first half and the last
half, with a marker counting what was left out between them.
"""

from __future__ import annotations

import collections
import random
import time

import pytest

from sandtrap import IsolationUnavailable, Policy, sandbox
from sandtrap.builtins import EndsBuffer, TailBuffer, make_output_buffer


def _write_all(buf, pieces):
    for piece in pieces:
        buf.write(piece)
    return "".join(pieces)


def _random_pieces(rng):
    return ["x" * rng.randint(0, 50) + str(i) for i in range(rng.randint(0, 200))]


@pytest.mark.parametrize("seed", range(50))
def test_the_tail_buffer_keeps_exactly_the_last_characters(seed):
    rng = random.Random(seed)
    cap = rng.randint(1, 400)
    buf = TailBuffer(cap)
    full = _write_all(buf, _random_pieces(rng))
    value = buf.getvalue()
    if len(full) <= cap:
        assert value == full
    else:
        assert value == TailBuffer._TRUNCATION_MARKER + full[-cap:]


@pytest.mark.parametrize("seed", range(50))
def test_the_ends_buffer_keeps_exactly_both_ends(seed):
    rng = random.Random(seed)
    cap = rng.randint(2, 400)
    buf = EndsBuffer(cap)
    full = _write_all(buf, _random_pieces(rng))
    value = buf.getvalue()
    if len(full) <= cap:
        assert value == full
    else:
        head, tail = cap // 2, cap - cap // 2
        omitted = len(full) - cap
        assert value == (
            f"{full[:head]}\n... [{omitted} characters omitted] ...\n{full[-tail:]}"
        )


def test_a_write_across_the_head_boundary_is_split():
    buf = EndsBuffer(10)
    buf.write("abc")
    buf.write("defghijklmnopqrstuvwxyz")
    assert buf.getvalue() == "abcde\n... [16 characters omitted] ...\nvwxyz"


def test_without_a_limit_both_keep_everything():
    for buf in (TailBuffer(), EndsBuffer()):
        assert _write_all(buf, ["a" * 1000, "b" * 1000]) == buf.getvalue()


def test_the_factory_picks_the_buffer():
    assert type(make_output_buffer(10)) is TailBuffer
    assert type(make_output_buffer(10, "tail")) is TailBuffer
    assert type(make_output_buffer(10, "ends")) is EndsBuffer


def test_stdout_keep_is_validated():
    with pytest.raises(ValueError, match="stdout_keep"):
        Policy(stdout_keep="middle")


@pytest.mark.parametrize("isolation", ["none", "process", "kernel"])
def test_stdout_and_stderr_keep_both_ends(isolation):
    """The policy crosses to a worker with the setting, and stderr follows
    it too."""
    policy = Policy(timeout=30.0, max_stdout=200, stdout_keep="ends")
    try:
        sb = sandbox(policy, isolation=isolation)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")
    with sb:
        result = sb.exec(
            "for i in range(1000):\n    print(f'line {i}')\n",
            argv=["x"],
        )
        errs = sb.exec(
            "import sys\nfor i in range(1000):\n    sys.stderr.write(f'err {i}\\n')\n",
            argv=["x"],
        )
    assert result.stdout.startswith("line 0\nline 1\n")
    assert "characters omitted" in result.stdout
    assert result.stdout.endswith("line 999\n")
    assert errs.stderr.startswith("err 0\n")
    assert errs.stderr.endswith("err 999\n")


def test_a_policy_from_before_stdout_keep_keeps_the_tail():
    """A policy unpickled from a release without the setting has no such
    attribute; it keeps the tail, as it did."""
    policy = Policy(timeout=30.0, max_stdout=50)
    del policy.stdout_keep
    with sandbox(policy) as sb:
        result = sb.exec("for i in range(100):\n    print(i)\n")
    assert result.stdout.startswith(TailBuffer._TRUNCATION_MARKER)
    assert result.stdout.endswith("99\n")


class _ShortLen(str):
    """A string that reports less than it holds."""

    def __len__(self):
        return 1


@pytest.mark.parametrize("cls", [TailBuffer, EndsBuffer])
def test_a_string_that_misstates_its_length_is_measured_by_its_content(cls):
    buf = cls(100)
    buf.write(_ShortLen("x" * 10_000))
    value = buf.getvalue()
    assert len(value) < 200
    assert "x" * 50 in value


@pytest.mark.parametrize("cls", [TailBuffer, EndsBuffer])
def test_only_strings_are_written(cls):
    with pytest.raises(TypeError, match="string argument expected"):
        cls(100).write(b"bytes")


def test_a_code_misstating_its_length_cannot_pass_the_cap():
    """End to end: sandboxed code writes such a string to sys.stdout."""
    policy = Policy(timeout=30.0, max_stdout=100)
    with sandbox(policy) as sb:
        result = sb.exec(
            "import sys\n"
            "class Short(str):\n"
            "    def __len__(self):\n"
            "        return 1\n"
            "sys.stdout.write(Short('x' * 100_000))\n",
            argv=["x"],
        )
    assert result.error is None, result.error
    assert len(result.stdout) < 200


class _YieldingDeque(collections.deque):
    """A deque that lets other threads run just after an item is read, as a
    thread switch at the worst moment would."""

    def __getitem__(self, index):
        item = super().__getitem__(index)
        time.sleep(1e-5)  # between reading the front and acting on it
        return item


@pytest.mark.parametrize("cls", [TailBuffer, EndsBuffer])
def test_concurrent_writers_keep_the_accounting(cls):
    """Writers from several threads (output routed from threads the code
    starts) trim the same buffer; each trim must see the others whole."""
    import threading

    buf = cls(1_000)
    buf._chunks = _YieldingDeque()
    errors = []

    def write(tag):
        try:
            for i in range(2_000):
                # lengths that differ, so a trim that removes the wrong
                # chunk shows in the accounting
                buf.write(tag * (i % 13 + 1) + "\n")
        except Exception as e:  # noqa: BLE001 - any failure is the finding
            errors.append(e)

    threads = [threading.Thread(target=write, args=(t,)) for t in "abcdefgh"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert buf._size == sum(len(c) for c in buf._chunks)
    assert buf._size <= buf._max


def test_empty_writes_do_not_grow_the_head():
    buf = EndsBuffer(100)
    for _ in range(10_000):
        buf.write("")
    assert buf._head == []
    buf.write("abc")
    assert buf.getvalue() == "abc"
