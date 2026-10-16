"""Captured output past ``max_stdout``: the tail, or both ends.

``Policy(stdout_keep="tail")`` (the default) keeps the most recent
``max_stdout`` characters; ``"ends"`` keeps the first half and the last
half, with a marker counting what was left out between them.
"""

from __future__ import annotations

import random

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
