"""``from __future__ import ...`` in sandboxed code: a directive to the
compiler, honoured as Python honours it, rather than an import."""

import asyncio

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
        assert "future feature braces is not defined" in str(result.error)


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
