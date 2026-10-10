"""``from __future__ import ...`` in sandboxed code: a directive to the
compiler, honoured as Python honours it, rather than an import."""

import asyncio
import sys

import pytest

from sandtrap import Policy, Sandbox, VirtualFS
from sandtrap.fs import IsolatedFS
from sandtrap.process import ProcessSandbox

POSTPONED = (
    "from __future__ import annotations\nclass C:\n    x: Nope\nprint('postponed')\n"
)
EAGER = "class C:\n    x: Nope\nprint('postponed')\n"


def test_annotations_are_postponed_when_the_code_asks():
    with Sandbox(Policy()) as sb:
        assert sb.exec(POSTPONED).stdout.strip() == "postponed"
        if sys.version_info < (3, 14):  # 3.14 evaluates them lazily anyway
            assert "Nope" in str(sb.exec(EAGER).error)


def test_an_async_execution_honours_it_too():
    """aexec wraps the body in a function, where a future import isn't
    allowed: it is taken off and compiled in as flags."""
    with Sandbox(Policy()) as sb:
        result = asyncio.run(sb.aexec(POSTPONED))
        assert (result.stdout.strip(), result.error) == ("postponed", None)


def test_after_a_docstring_as_python_allows():
    with Sandbox(Policy()) as sb:
        result = sb.exec('"""A script."""\n' + POSTPONED)
        assert (result.stdout.strip(), result.error) == ("postponed", None)


def test_a_late_future_import_is_refused():
    with Sandbox(Policy()) as sb:
        result = sb.exec("x = 1\n" + POSTPONED)
        assert "must occur at the beginning of the file" in str(result.error)


def test_a_feature_python_lacks_is_a_syntax_error():
    with Sandbox(Policy()) as sb:
        result = sb.exec("from __future__ import braces\n")
        assert isinstance(result.error, SyntaxError)
        # 3.14's parser refuses this one itself, in its own words
        assert "braces" in str(result.error) or "not a chance" in str(result.error)


def test_a_workspace_module_honours_it():
    fs = VirtualFS({})
    fs.write("/shapes.py", POSTPONED.replace("print('postponed')\n", "").encode())
    with Sandbox(Policy(), filesystem=fs) as sb:
        result = sb.exec("import shapes\nprint(shapes.C.__name__)")
        assert (result.stdout.strip(), result.error) == ("C", None)


def test_under_process_isolation(tmp_path):
    with ProcessSandbox(
        Policy(timeout=10.0), filesystem=IsolatedFS(str(tmp_path))
    ) as ps:
        result = ps.exec(POSTPONED)
        assert (result.stdout.strip(), result.error) == ("postponed", None)


@pytest.mark.parametrize("names", ["annotations", "annotations, division"])
def test_more_than_one_feature(names):
    with Sandbox(Policy()) as sb:
        src = POSTPONED.replace("import annotations", f"import {names}")
        assert sb.exec(src).stdout.strip() == "postponed"


CLASSVAR = """\
from __future__ import annotations
import typing

class Counter:
    total: typing.ClassVar[int] = 0

    def step(self, by: typing.Optional[int] = None) -> typing.List[int]:
        return []
"""

WRITTEN = {"by": "typing.Optional[int]", "return": "typing.List[int]"}


def _typed_policy():
    import typing

    policy = Policy()
    policy.module(typing)
    return policy


def test_a_postponed_annotation_is_stored_as_written():
    """Rewritten, ``typing.ClassVar[int]`` would be stored as a gate call,
    which ``dataclasses`` and anything else reading annotations doesn't
    recognise."""
    with Sandbox(_typed_policy()) as sb:
        result = sb.exec(CLASSVAR)
        assert result.error is None, result.error
        counter = result.namespace["Counter"]
        assert counter.__annotations__ == {"total": "typing.ClassVar[int]"}
        assert counter.step.__wrapped__.__annotations__ == WRITTEN


def test_a_workspace_module_stores_them_as_written_too():
    fs = VirtualFS({})
    fs.write("/counters.py", CLASSVAR.encode())
    with Sandbox(_typed_policy(), filesystem=fs) as sb:
        result = sb.exec("from counters import Counter")
        assert result.error is None, result.error
        counter = result.namespace["Counter"]
        assert counter.__annotations__ == {"total": "typing.ClassVar[int]"}


def test_a_postponed_annotation_is_still_checked():
    with Sandbox(Policy()) as sb:
        result = sb.exec("from __future__ import annotations\nx: __builtins__ = 1\n")
        assert result.error is not None
