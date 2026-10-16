"""Select outputs before crossing the worker boundary, without changing defaults."""

import pytest

from sandtrap import Policy, sandbox


@pytest.mark.parametrize("isolation", ["none", "process"])
def test_result_selection_is_per_call_and_keeps_stdout(isolation):
    with sandbox(Policy(), isolation=isolation) as sb:
        result = sb.exec(
            "scratch = bytearray(1000000)\nui = {'answer': 42}\nprint('done')",
            result_names=("ui",),
        )
        assert result.error is None
        assert result.namespace == {"ui": {"answer": 42}}
        assert result.stdout == "done\n"
        assert sb.exec("scratch = 3").namespace == {"scratch": 3}
        assert sb.exec("scratch = 3", result_names=()).namespace == {}


@pytest.mark.parametrize("isolation", ["none", "process"])
@pytest.mark.asyncio
async def test_async_result_selection(isolation):
    with sandbox(Policy(), isolation=isolation) as sb:
        result = await sb.aexec("scratch = 1\nui = 2", result_names=("ui",))
        assert result.error is None
        assert result.namespace == {"ui": 2}


def test_memory_failure_does_not_transport_locals_or_print_snapshots():
    with sandbox(Policy(), isolation="process") as sb:
        result = sb.exec(
            "scratch = list(range(100))\nprint(scratch)\nraise MemoryError('budget')"
        )
        assert isinstance(result.error, MemoryError)
        assert str(result.error) == "budget"
        assert result.namespace == {}
        assert result.prints == []
        assert "[0, 1, 2" in result.stdout
        # An ordinary exception still returns locals for debugging by default.
        result = sb.exec("answer = 42\nraise ValueError('ordinary')")
        assert isinstance(result.error, ValueError)
        assert result.namespace["answer"] == 42


@pytest.mark.parametrize("isolation", ["none", "process"])
def test_invalid_result_names_fail_on_the_calling_side(isolation):
    with sandbox(Policy(), isolation=isolation) as sb:
        with pytest.raises(TypeError, match="result_names"):
            sb.exec("answer = 42", result_names="answer")
