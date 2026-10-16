"""Print values rendered as they are printed (``render_prints``).

Each print, echoed expression and help() is rendered by reprobate at the
moment it is made, into ``result.rendered_prints``: bounded text, with no
copy of the values and, out of a worker, only text crossing.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("reprobate")

from sandtrap import (  # noqa: E402
    IsolatedFS,
    IsolationUnavailable,
    Policy,
    PrintRendering,
    sandbox,
)

ISOLATIONS = ("none", "process", "kernel")
RUNNERS = ("exec", "aexec")


def _sandbox(isolation, *, rendering=PrintRendering(), policy=None, **kwargs):
    try:
        return sandbox(
            policy or Policy(timeout=30.0),
            isolation=isolation,
            render_prints=rendering,
            **kwargs,
        )
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


def _run(sb, runner, source, **kwargs):
    if runner == "aexec":
        return asyncio.run(sb.aexec(source, **kwargs))
    return sb.exec(source, **kwargs)


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_each_print_is_rendered_as_it_is_made(isolation, runner):
    with _sandbox(isolation) as sb:
        result = _run(sb, runner, "print('total', 3)\nprint(list(range(5)))\n")
    assert result.error is None, result.error
    assert result.rendered_prints == ["'total' 3", "[0, 1, 2, 3, 4]"]
    assert result.print_count == 2
    assert result.stdout == "total 3\n[0, 1, 2, 3, 4]\n"
    assert result.prints == []  # no snapshots were asked for


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_echoed_expression_and_help_are_prints_too(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec("x = {'a': 1}\nx\n", echo="last")
        helped = sb.exec("help(len)\n")
    assert result.rendered_prints == ["{'a': 1}"]
    assert helped.print_count == 1
    assert "len" in helped.rendered_prints[0]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_rendering_stays_within_its_budget(isolation):
    rendering = PrintRendering(per_print=120)
    with _sandbox(isolation, rendering=rendering) as sb:
        result = sb.exec(
            "print(list(range(100_000)))\nprint('a' * 10_000, 'b' * 10_000)\n"
        )
    assert result.error is None, result.error
    big, pair = result.rendered_prints
    assert len(big) <= 120 and "more" in big
    # two values share the print's budget, each within its share
    assert len(pair) <= 120 + 1


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_prints_past_the_limit_are_only_counted(isolation):
    with _sandbox(isolation, rendering=PrintRendering(limit=3)) as sb:
        result = sb.exec("for i in range(10):\n    print(i)\n")
    assert result.rendered_prints == ["0", "1", "2"]
    assert result.print_count == 10
    assert result.stdout.splitlines() == [str(i) for i in range(10)]


def test_rendering_copies_nothing():
    """A printed 50MB value is rendered where it is; snapshots copy it."""
    import tracemalloc

    def peak(sb):
        tracemalloc.start()
        try:
            result = sb.exec("b = bytearray(50 << 20)\nprint(b)\ndel b\n", keep=())
            assert result.error is None, result.error
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    with sandbox(Policy(timeout=30.0), snapshot_prints=True) as copying:
        copied = peak(copying)
    with _sandbox("none") as rendering:
        rendered = peak(rendering)
    assert rendered < copied - (40 << 20)


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_memory_error_keeps_its_renderings(isolation):
    """Unlike snapshots, renderings are bounded text, like stdout."""
    with _sandbox(isolation, snapshot_prints=True) as sb:
        result = sb.exec("print('before')\nraise MemoryError('synthetic')\n")
    assert isinstance(result.error, MemoryError)
    assert result.prints == []
    assert result.rendered_prints == ["'before'"]


def test_a_librarys_value_renders_in_a_restricted_worker(tmp_path):
    """Kernel isolation with an IsolatedFS root confines what the worker can
    read once its code runs: rendering a library's value must need nothing
    that was not imported before. (A NumPy array, whose own repr needs
    nothing late either, so only the rendering is under test.)"""
    pytest.importorskip("numpy")
    import numpy

    policy = Policy(timeout=30.0)
    policy.module(numpy, name="np")
    root = tmp_path / "root"
    root.mkdir()
    with _sandbox("kernel", policy=policy, filesystem=IsolatedFS(str(root))) as sb:
        result = sb.exec("print(np.arange(1000))\n")
    assert result.error is None, result.error
    assert result.rendered_prints[0].startswith("ndarray(1000, int64")


def test_without_render_prints_nothing_is_rendered():
    with sandbox(Policy(timeout=30.0)) as sb:
        result = sb.exec("print(1)\n")
    assert result.rendered_prints == []
    assert result.print_count == 0


def test_a_value_reprobate_cannot_render_is_named_by_its_type(monkeypatch):
    import reprobate

    from sandtrap.prints import PrintRenderer

    def broken(value, budget):
        raise RuntimeError("no")

    monkeypatch.setattr(reprobate, "render", broken)
    renderer = PrintRenderer(PrintRendering())
    renderer.append((3, "x"))
    assert renderer.rendered == ["<int> <str>"]


@pytest.mark.parametrize(
    "value, error",
    [("yes", TypeError), (PrintRendering, TypeError), (3, TypeError)],
)
def test_render_prints_takes_a_print_rendering(value, error):
    with pytest.raises(error, match="PrintRendering"):
        sandbox(Policy(timeout=30.0), render_prints=value)


@pytest.mark.parametrize("field", ["per_print", "limit"])
@pytest.mark.parametrize("bad", [0, -1, 2.5, True])
def test_print_rendering_bounds_are_positive_ints(field, bad):
    with pytest.raises(ValueError, match=field):
        PrintRendering(**{field: bad})
