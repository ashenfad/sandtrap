"""Tests for the unified sandbox() factory."""

import pytest
from monkeyfs import IsolatedFS, VirtualFS

from sandtrap import Policy, sandbox
from sandtrap.process.sandbox import ProcessSandbox
from sandtrap.sandbox import Sandbox


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


# ------------------------------------------------------------------
# isolation="none" (in-process)
# ------------------------------------------------------------------


def test_none_returns_sandbox():
    sb = sandbox(Policy(timeout=5.0))
    assert isinstance(sb, Sandbox)


def test_none_basic_exec():
    with sandbox(Policy(timeout=5.0)) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_none_with_filesystem():
    fs = VirtualFS({})
    fs.write("/data.txt", b"hello")
    with sandbox(Policy(timeout=5.0), filesystem=fs) as sb:
        result = sb.exec("content = open('/data.txt').read()")
        assert result.error is None
        assert result.namespace["content"] == "hello"


def test_none_with_print_handler():
    output = []
    with sandbox(
        Policy(timeout=5.0), print_handler=lambda *a, **kw: output.append(a)
    ) as sb:
        sb.exec("print('hello')")
    assert len(output) == 1
    assert output[0] == ("hello",)


# ------------------------------------------------------------------
# isolation="process" (subprocess, no kernel)
# ------------------------------------------------------------------


def test_process_returns_process_sandbox():
    sb = sandbox(Policy(timeout=5.0), isolation="process")
    assert isinstance(sb, ProcessSandbox)


def test_process_basic_exec(root):
    with sandbox(
        Policy(timeout=5.0), isolation="process", filesystem=IsolatedFS(root)
    ) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_process_no_filesystem():
    with sandbox(Policy(timeout=5.0), isolation="process") as sb:
        result = sb.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


def test_process_with_virtualfs():
    fs = VirtualFS({})
    fs.write("/data.txt", b"hello")
    with sandbox(Policy(timeout=5.0), isolation="process", filesystem=fs) as sb:
        result = sb.exec("content = open('/data.txt').read()")
        assert result.error is None
        assert result.namespace["content"] == "hello"


# ------------------------------------------------------------------
# isolation="kernel" (subprocess + kernel restrictions)
# ------------------------------------------------------------------


def test_kernel_returns_process_sandbox():
    sb = sandbox(Policy(timeout=5.0), isolation="kernel")
    assert isinstance(sb, ProcessSandbox)


def test_kernel_basic_exec(root):
    with sandbox(
        Policy(timeout=5.0), isolation="kernel", filesystem=IsolatedFS(root)
    ) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_kernel_no_filesystem():
    with sandbox(Policy(timeout=5.0), isolation="kernel") as sb:
        result = sb.exec("x = 42")
        assert result.error is None
        assert result.namespace["x"] == 42


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


def test_print_handler_with_process_raises():
    with pytest.raises(ValueError, match="print_handler.*isolation"):
        sandbox(Policy(), isolation="process", print_handler=lambda *a: None)


def test_print_handler_with_kernel_raises():
    with pytest.raises(ValueError, match="print_handler.*isolation"):
        sandbox(Policy(), isolation="kernel", print_handler=lambda *a: None)


# ------------------------------------------------------------------
# mode parameter
# ------------------------------------------------------------------


def test_mode_raw_none():
    with sandbox(Policy(timeout=5.0), mode="raw") as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5


def test_mode_raw_process(root):
    with sandbox(
        Policy(timeout=5.0),
        mode="raw",
        isolation="process",
        filesystem=IsolatedFS(root),
    ) as sb:
        result = sb.exec("x = 2 + 3")
        assert result.error is None
        assert result.namespace["x"] == 5
