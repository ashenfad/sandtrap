"""Tests for REPL-style auto-display of top-level expressions (echo)."""

import pytest

from sandtrap import Policy, sandbox


@pytest.fixture
def policy():
    return Policy(timeout=5.0, tick_limit=100_000)


class TestEchoDefaults:
    def test_off_by_default(self, policy):
        """Without echo, a bare expression produces no output at all."""
        with sandbox(policy, snapshot_prints=True) as sb:
            result = sb.exec("x = 1\nx")
        assert result.stdout == ""
        assert result.prints == []

    def test_factory_default_is_none(self, policy):
        with sandbox(policy) as sb:
            result = sb.exec("42")
        assert result.stdout == ""


class TestEchoAll:
    def test_bare_expression_echoes_repr(self, policy):
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("x = 41 + 1\nx")
        assert result.stdout == "42\n"

    def test_string_echoes_with_quotes(self, policy):
        """Display is repr, not str — REPL convention. A lone string is
        a value query, not a docstring."""
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("'hello'")
        assert result.stdout == "'hello'\n"

    def test_print_does_not_double_echo(self, policy):
        """print(x) is itself a top-level expression returning None;
        None-suppression keeps it to a single output entry."""
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("print('once')")
        assert result.stdout == "once\n"

    def test_bare_none_is_suppressed(self, policy):
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("None")
        assert result.stdout == ""

    def test_ordering_interleaves_with_print(self, policy):
        """Displays and prints land in both channels in execution order."""
        with sandbox(policy, echo="all", snapshot_prints=True) as sb:
            result = sb.exec("1\nprint('two')\n3")
        assert result.stdout == "1\ntwo\n3\n"
        assert result.prints == [(1,), ("two",), (3,)]

    def test_prints_channel_gets_raw_object(self, policy):
        """The snapshot channel carries the object itself (an implicit
        single-arg print), not its repr — downstream renderers rely on
        receiving raw values."""
        with sandbox(policy, echo="all", snapshot_prints=True) as sb:
            result = sb.exec("[1, 2, 3]")
        assert result.prints == [([1, 2, 3],)]
        assert result.prints[0][0] == [1, 2, 3]

    def test_top_level_only(self, policy):
        """Expressions inside functions, loops, and if-blocks never echo."""
        with sandbox(policy, echo="all") as sb:
            result = sb.exec(
                "def f():\n"
                "    99\n"
                "    return None\n"
                "for i in range(3):\n"
                "    i\n"
                "if True:\n"
                "    'inner'\n"
                "f()\n"
            )
        assert result.stdout == ""

    def test_call_result_echoes_like_repl(self, policy):
        """A top-level call echoes its non-None return value."""
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("def f():\n    return 7\nf()")
        assert result.stdout == "7\n"

    def test_module_docstring_not_echoed(self, policy):
        with sandbox(policy, echo="all") as sb:
            result = sb.exec('"""module doc"""\n5')
        assert result.stdout == "5\n"

    def test_attribute_expression_is_gated(self, policy):
        """The displayed expression still routes through the attribute
        gate — a blocked private attribute raises, not echoes."""
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("x = 5\nx.__class__")
        assert result.error is not None
        assert result.stdout == ""

    def test_namespace_unaffected(self, policy):
        """Echo wrapping doesn't alter the result namespace."""
        with sandbox(policy, echo="all") as sb:
            result = sb.exec("x = 1\nx\ny = 2")
        assert result.namespace == {"x": 1, "y": 2}


class TestEchoLast:
    def test_final_expression_echoes(self, policy):
        with sandbox(policy, echo="last") as sb:
            result = sb.exec("a = 1\na\nb = 2\nb")
        assert result.stdout == "2\n"

    def test_no_echo_when_final_statement_is_not_expression(self, policy):
        """Jupyter last_expr semantics: earlier bare expressions don't
        echo when the module ends on a non-expression statement."""
        with sandbox(policy, echo="last") as sb:
            result = sb.exec("a = 1\na\nb = 2")
        assert result.stdout == ""

    def test_lone_string_echoes_even_under_last(self, policy):
        """A one-statement string program is a value query (echoes);
        only a leading string *followed by code* is a docstring."""
        with sandbox(policy, echo="last") as sb:
            result = sb.exec("'just a value'")
        assert result.stdout == "'just a value'\n"

    def test_leading_docstring_skipped_when_code_follows(self, policy):
        with sandbox(policy, echo="last") as sb:
            result = sb.exec('"""doc"""\nx = 1')
        assert result.stdout == ""


class TestEchoModes:
    def test_raw_mode(self, policy):
        with sandbox(policy, echo="all", mode="raw") as sb:
            result = sb.exec("x = 2\nx * 3")
        assert result.stdout == "6\n"

    @pytest.mark.asyncio
    async def test_aexec(self, policy):
        """Display wrapping happens before aexec's async-fn wrapping, so
        top-level echo survives the transform."""
        with sandbox(policy, echo="all") as sb:
            result = await sb.aexec("1 + 1")
        assert result.stdout == "2\n"


class TestEchoValidation:
    def test_invalid_value_raises(self, policy):
        with pytest.raises(ValueError, match="Invalid echo option"):
            sandbox(policy, echo="lastt")

    def test_none_raises_instead_of_enabling_echo(self, policy):
        """echo=None reads as 'off' but the rewriter's dispatch would
        treat it as 'all' — it must raise, never silently activate."""
        with pytest.raises(ValueError, match="Invalid echo option"):
            sandbox(policy, echo=None)

    def test_process_sandbox_raises_at_construction(self, policy):
        """ProcessSandbox validates in the host constructor — not as a
        wrapped worker-init failure at first exec."""
        from sandtrap.process.sandbox import ProcessSandbox

        with pytest.raises(ValueError, match="Invalid echo option"):
            ProcessSandbox(policy, isolation="none", echo="everything")


class TestEchoProcessIsolation:
    def test_echo_crosses_process_boundary(self, policy):
        with sandbox(
            policy, isolation="process", echo="all", snapshot_prints=True
        ) as sb:
            result = sb.exec("7 * 6")
        assert result.stdout == "42\n"
        assert result.prints == [(42,)]

    def test_default_off_in_process_mode(self, policy):
        with sandbox(policy, isolation="process") as sb:
            result = sb.exec("7 * 6")
        assert result.stdout == ""


class TestPerExecOverride:
    """One sandbox, two surfaces: a notebook-style caller passes
    echo="last" per call while a script-semantics caller passes
    echo="none" — without paying for two sandboxes (two workers,
    under process isolation)."""

    def test_exec_can_enable_echo_on_a_quiet_sandbox(self, policy):
        with sandbox(policy, snapshot_prints=True) as sb:
            result = sb.exec("x = 41\nx + 1", echo="last")
            assert result.stdout == "42\n"
            assert result.prints == [(42,)]
            # and the override is per-CALL: the default is untouched
            result = sb.exec("x = 41\nx + 1")
            assert result.stdout == ""

    def test_exec_can_silence_an_echoing_sandbox(self, policy):
        with sandbox(policy, echo="last") as sb:
            result = sb.exec("1 + 1", echo="none")
            assert result.stdout == ""
            result = sb.exec("1 + 1")
            assert result.stdout == "2\n"

    def test_override_crosses_the_process_boundary(self, policy):
        with sandbox(policy, isolation="process", snapshot_prints=True) as sb:
            result = sb.exec("7 * 6", echo="last")
            assert result.stdout == "42\n"
            assert result.prints == [(42,)]
            result = sb.exec("7 * 6")
            assert result.stdout == ""

    def test_invalid_override_raises_before_running(self, policy):
        with sandbox(policy) as sb:
            with pytest.raises(ValueError, match="Invalid echo option"):
                sb.exec("1", echo="loud")

    def test_invalid_override_raises_host_side_in_process_mode(self, policy):
        """Bad per-exec echo on a ProcessSandbox fails as a plain
        ValueError in the caller — not as worker-error noise."""
        with sandbox(policy, isolation="process") as sb:
            with pytest.raises(ValueError, match="Invalid echo option"):
                sb.exec("1", echo="loud")

    @pytest.mark.asyncio
    async def test_aexec_honors_the_override(self, policy):
        with sandbox(policy) as sb:
            result = await sb.aexec("1 + 1", echo="last")
        assert result.stdout == "2\n"
