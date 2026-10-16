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
    """A printed 48MB DataFrame is rendered where it is; a snapshot copies
    it. (Its own printed repr is short, so stdout costs the same either way.)

    Measured in a fresh interpreter: allocating here would raise this
    process's peak RSS, which an in-process memory limit is measured from,
    and loosen the limits of the tests that follow."""
    import subprocess
    import sys
    import textwrap

    pytest.importorskip("pandas")
    script = textwrap.dedent(
        """
        import tracemalloc
        import pandas
        from sandtrap import Policy, PrintRendering, sandbox

        def peak(**options):
            policy = Policy(timeout=60.0)
            policy.module(pandas, name="pd")
            df = pandas.DataFrame({"a": range(6_000_000)})
            with sandbox(policy, **options) as sb:
                tracemalloc.start()
                try:
                    result = sb.exec("print(df)", namespace={"df": df}, keep=())
                    assert result.error is None, result.error
                    return tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()

        print(peak(snapshot_prints=True), peak(render_prints=PrintRendering()))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    copied, rendered = map(int, done.stdout.split())
    assert copied > 40 << 20  # the snapshot copied the frame
    assert rendered < 8 << 20  # the rendering did not


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


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_print_of_many_values_stays_within_its_budget(isolation):
    """The budget holds for the whole print, not each value: values past it
    are counted, not rendered."""
    with _sandbox(isolation, rendering=PrintRendering(per_print=80)) as sb:
        result = sb.exec("print(*range(100_000))\n")
    (rendered,) = result.rendered_prints
    assert len(rendered) <= 80
    assert rendered.startswith("0 1 2 ") and rendered.endswith(" more")


def test_rendering_work_is_bounded_by_the_budget(monkeypatch):
    import reprobate

    calls = []
    real = reprobate.render

    def counting(value, budget):
        calls.append(budget)
        return real(value, budget=budget)

    monkeypatch.setattr(reprobate, "render", counting)
    with _sandbox("none", rendering=PrintRendering(per_print=80)) as sb:
        sb.exec("print(*range(100_000))\n")
    assert len(calls) <= 80


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_rendering_does_not_change_what_print_does(isolation):
    """The print happens first: a value whose repr works once is printed
    as it would be without rendering, and its rendering falls back."""
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "class Once:\n"
            "    calls = 0\n"
            "    def __repr__(self):\n"
            "        Once.calls += 1\n"
            "        if Once.calls > 1:\n"
            "            raise RuntimeError('again')\n"
            "        return 'once'\n"
            "print(Once())\n"
        )
    assert result.error is None, result.error
    assert result.stdout == "once\n"
    assert result.rendered_prints == ["<Once>"]


def test_exec_result_keeps_its_positional_order():
    """Fields added later come last, so a result built positionally keeps
    meaning what it meant."""
    from sandtrap import ExecResult

    result = ExecResult({}, "out", "", None, 0, [], None, ("x",), {"v": 3})
    assert result.dropped == ("x",)
    assert result.sizes == {"v": 3}
    assert result.rendered_prints == [] and result.print_count == 0
