"""A module's metadata dunders read as data.

`__version__`, `__author__` and `__all__` are plain values a module body
binds, and the way a package's own modules learn its version. The dunder
rule refused them with the module type's machinery; they now read --
out of the module's own namespace, without running anything, and only
as an exact str (or for `__all__` a list of them, copied).
"""

import types

import pytest

from sandtrap import Policy, Sandbox, VirtualFS

TERN = {
    "/tern/__init__.py": (
        b"__version__ = '0.1.0'\n__author__ = 'Ada'\n__all__ = ['main']\n"
    ),
    "/tern/cli.py": (
        b"from tern import __version__\n\ndef main():\n    return __version__\n"
    ),
}


def _run(code, files=TERN, policy=None):
    fs = VirtualFS({})
    for path, source in files.items():
        fs.write(path, source)
    return Sandbox(policy or Policy(), filesystem=fs).exec(code)


@pytest.mark.parametrize(
    "code, expected",
    [
        ("import tern\nresult = tern.__version__", "0.1.0"),
        ("from tern import __version__ as result", "0.1.0"),
        ("from tern.cli import main\nresult = main()", "0.1.0"),
        ("from tern import __author__ as result", "Ada"),
        ("import tern\nresult = tern.__author__", "Ada"),
        ("from tern import __all__ as result", ["main"]),
        ("import tern\nresult = tern.__all__", ["main"]),
    ],
)
def test_a_workspace_packages_metadata_reads(code, expected):
    result = _run(code)
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["result"] == expected


def test_metadata_that_is_not_plain_data_is_refused():
    """A tuple version, or an object posing as one, is not read: only an
    exact str carries no attribute surface of its own."""
    files = {
        "/m.py": (
            b"class V(str):\n    pass\n"
            b"__version__ = V('1')\n__author__ = ('a', 'b')\n__all__ = 'main'\n"
        )
    }
    for code in (
        "from m import __version__",
        "import m\nm.__version__",
        "from m import __author__",
        "from m import __all__",
    ):
        assert _run(code, files).error is not None, code


def test_a_module_getattr_is_never_run_for_metadata():
    """PEP 562 `__getattr__` could compute anything on demand; a name the
    module did not bind is simply not there."""
    files = {
        "/m.py": (
            b"calls = []\n"
            b"def __getattr__(name):\n    calls.append(name)\n    return 'computed'\n"
        )
    }
    result = _run(
        "import m\ntry:\n    m.__version__\nexcept AttributeError:\n    pass\n"
        "result = list(m.calls)",
        files,
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["result"] == []


def test_metadata_is_not_writable_from_outside():
    result = _run("import tern\ntern.__version__ = '9'")
    assert isinstance(result.error, AttributeError)
    result = _run("import tern\ndel tern.__all__")
    assert isinstance(result.error, AttributeError)


def test_metadata_reads_on_modules_only():
    """`__version__` on a class or instance is not module metadata, and
    stays refused like any other dunder."""
    files = {"/m.py": b"class C:\n    __version__ = '1'\n"}
    assert _run("from m import C\nC.__version__", files).error is not None
    assert _run("from m import C\nC().__version__", files).error is not None


def _host_module():
    mod = types.ModuleType("hostlib")
    mod.__version__ = "2.0"
    mod.__all__ = ["thing"]
    mod.thing = 1
    return mod


def test_a_granted_host_modules_metadata_reads_past_its_filters(monkeypatch):
    """A module grant's default exclude ("_*") would hide the names; they
    read the same as on a workspace module, and `__all__` is a copy."""
    import sys

    mod = _host_module()
    monkeypatch.setitem(sys.modules, "hostlib", mod)
    policy = Policy()
    policy.module(mod)
    result = _run(
        "import hostlib\nfrom hostlib import __version__, __all__ as names\n"
        "names.append('evil')\n"
        "result = (hostlib.__version__, __version__, hostlib.__all__)",
        files={},
        policy=policy,
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["result"] == ("2.0", "2.0", ["thing"])
    assert mod.__all__ == ["thing"]  # the host's own list is untouched


@pytest.mark.parametrize("isolation", ("none", "process", "kernel"))
def test_metadata_reads_at_every_isolation(isolation):
    from sandtrap import IsolationUnavailable, sandbox

    fs = VirtualFS({})
    for path, source in TERN.items():
        fs.write(path, source)
    try:
        sb = sandbox(Policy(timeout=30.0), isolation=isolation, filesystem=fs)
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")
    with sb as active:
        result = active.exec(
            "import tern\nfrom tern.cli import main\n"
            "result = (tern.__version__, main(), tern.__all__)"
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["result"] == ("0.1.0", "0.1.0", ["main"])
