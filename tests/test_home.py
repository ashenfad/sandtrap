"""Sandboxed code runs in a module of its own (``sandtrap.home``): what
Python finds a class's home by finds it, and what the code defines still
can't leave the sandbox by pickle."""

import dataclasses
import pickle
import sys
import typing

import pytest

from sandtrap import Policy, RpcProxyMarker, Sandbox, sandbox
from sandtrap.home import SandboxObjectError, check_sendable, is_sandbox_module

NODE = """\
from dataclasses import dataclass

@dataclass
class Node:
    label: str
    child: "Node | None"

tree = Node("a", Node("b", None))
print(tree.child.label, __name__)
"""


def policy(**kw):
    p = Policy(timeout=10.0, **kw)
    p.module(dataclasses)
    return p


def test_a_quoted_annotation_resolves_in_a_dataclass():
    with Sandbox(policy()) as sb:
        result = sb.exec(NODE)
        assert result.error is None, result.error
        label, name = result.stdout.split()
        assert label == "b" and is_sandbox_module(name) and name == sb.module_name
        node = result.namespace["Node"]
        assert node.__module__ == sb.module_name
        # the host can resolve the class's annotations while the sandbox
        # is in use, as anything finding a class's home can
        assert typing.get_type_hints(node)["child"] == node | None


def test_the_module_is_in_use_only_while_the_sandbox_is():
    sb = Sandbox(policy())
    with sb:
        sb.exec("x = 1")
        assert sb.module_name in sys.modules
    assert sb.module_name not in sys.modules


def test_each_sandbox_has_a_module_of_its_own():
    with Sandbox(policy()) as one, Sandbox(policy()) as two:
        assert one.module_name != two.module_name
        one.exec("class A: pass")
        two.exec("class B: pass")
        assert sys.modules[one.module_name] is not sys.modules[two.module_name]


def test_a_name_the_embedder_gives_is_left_alone():
    with Sandbox(policy()) as sb:
        result = sb.exec("print(__name__)", namespace={"__name__": "__main_like__"})
        assert result.stdout.strip() == "__main_like__"
        assert "__main_like__" not in sys.modules


def test_the_modules_own_names_stay_out_of_the_result():
    with Sandbox(policy()) as sb:
        names = set(sb.exec('"""A script."""\nx = 1').namespace)
        assert names == {"x"}


# -- what can't leave by pickle -------------------------------------------------------


def test_the_codes_own_classes_and_functions_are_refused():
    with Sandbox(policy()) as sb:
        ns = sb.exec("class C: pass\ndef f(): pass\nc = C()").namespace
        for value in (ns["C"], ns["f"], ns["c"], [ns["c"]]):
            with pytest.raises(
                SandboxObjectError, match="defined in the sandboxed code"
            ):
                check_sendable(value)


def test_data_that_merely_mentions_a_sandbox_module_is_sent():
    raw = check_sendable({"note": "__sandtrap_1__ is a module name"})
    assert pickle.loads(raw) == {"note": "__sandtrap_1__ is a module name"}


@pytest.fixture
def process():
    with sandbox(
        policy(),
        isolation="process",
        rpc_handlers={"echo": lambda method, args, kwargs: args[0]},
    ) as sb:
        yield sb


def test_a_quoted_annotation_resolves_in_a_worker(process):
    result = process.exec(NODE)
    assert result.error is None, result.error
    assert result.stdout.split()[0] == "b"


def test_a_value_of_the_codes_own_class_stays_in_the_worker(process):
    """As before: it is dropped from the result, by name, and the rest
    comes back."""
    result = process.exec(NODE + "count = 2\n")
    assert result.namespace["count"] == 2
    assert "tree" not in result.namespace and "Node" not in result.namespace


def test_an_error_of_the_codes_own_class_comes_back_as_its_story(process):
    result = process.exec("class Oops(Exception): pass\nraise Oops('mine')")
    assert result.error is not None
    assert "Oops: mine" in str(result.error)


def test_a_host_call_with_the_codes_own_value_is_refused_in_the_code(process):
    result = process.exec(
        "class C: pass\n"
        "try:\n"
        "    echo.send(C())\n"
        "except Exception as error:\n"
        "    print(type(error).__name__, error)\n"
        "print(echo.send({'plain': [1, 2]}))\n",
        namespace={"echo": RpcProxyMarker(target="echo")},
    )
    assert result.error is None, result.error
    first, second = result.stdout.strip().splitlines()
    assert first.startswith(
        "SandboxObjectError class 'C' is defined in the sandboxed code"
    )
    assert second == "{'plain': [1, 2]}"
