"""A registration's grants reach the host code that reading one of its
attributes runs: a property getter, a module's ``__getattr__``, the lookup
behind a from-import. And reading an attribute runs that code once, only
for a member the policy lets through."""

import functools
import importlib
import os
import socket
import sys
import time
import types

import pytest
from monkeyfs import VirtualFS

from sandtrap import MemberSpec, Policy, Sandbox
from sandtrap.clock import observe_pauses
from sandtrap.errors import StTimeout
from sandtrap.gates import _resolution_runs_code
from sandtrap.net.patch import install as install_net

DENIED = "Network access denied"


@pytest.fixture(autouse=True)
def _install_net_patches():
    install_net()


def _resolve() -> bool:
    """A gated network operation that needs no server."""
    return bool(socket.getaddrinfo("127.0.0.1", 80, type=socket.SOCK_STREAM))


class Client:
    def resolve(self) -> bool:
        return _resolve()

    @property
    def addresses(self) -> bool:
        return _resolve()

    @property
    def other(self) -> bool:
        return _resolve()

    @functools.cached_property
    def cached(self) -> bool:
        return _resolve()

    @property
    def failing(self) -> bool:
        _resolve()
        raise ValueError("getter failed")


class Ungranted:
    @property
    def addresses(self) -> bool:
        return _resolve()


def _exec(policy: Policy, code: str, **namespace):
    return Sandbox(policy).exec(code, namespace=namespace)


# -- network ------------------------------------------------------------------


@pytest.mark.parametrize("member", ["resolve()", "addresses", "cached"])
def test_a_class_grant_reaches_its_getters(member):
    policy = Policy()
    policy.cls(Client, network_access=True)
    result = _exec(policy, f"value = c.{member}", c=Client())
    assert result.error is None
    assert result.namespace["value"] is True


def test_a_getter_without_a_grant_is_still_denied():
    policy = Policy()
    policy.cls(Ungranted)
    result = _exec(policy, "value = u.addresses", u=Ungranted())
    assert DENIED in str(result.error)


def test_a_member_grant_reaches_that_getter_only():
    policy = Policy()
    policy.cls(Client, configure={"addresses": MemberSpec(network_access=True)})
    result = _exec(policy, "a = c.addresses\nb = c.other", c=Client())
    assert result.namespace["a"] is True
    assert DENIED in str(result.error)


def test_the_grant_ends_with_the_getter():
    policy = Policy()
    policy.cls(Client, network_access=True)
    policy.cls(Ungranted)
    result = _exec(
        policy, "a = c.addresses\nb = u.addresses", c=Client(), u=Ungranted()
    )
    assert result.namespace["a"] is True
    assert DENIED in str(result.error)


def test_the_grant_ends_when_the_getter_raises():
    policy = Policy()
    policy.cls(Client, network_access=True)
    policy.cls(Ungranted)
    code = """\
try:
    c.failing
except ValueError:
    raised = True
b = u.addresses
"""
    result = _exec(policy, code, c=Client(), u=Ungranted())
    assert result.namespace["raised"] is True
    assert DENIED in str(result.error)


# The attribute read and the from-import resolve the name through two gates.
LAZY_READS = [
    "import lazymod\nvalue = lazymod.lazy",
    "from lazymod import lazy as value",
]


def _lazy_module(name: str) -> types.ModuleType:
    """A module that answers ``lazy`` from a PEP 562 ``__getattr__``."""
    mod = types.ModuleType(name)

    def __getattr__(attr: str) -> bool:
        if attr == "lazy":
            return _resolve()
        raise AttributeError(attr)

    mod.__getattr__ = __getattr__
    return mod


@pytest.mark.parametrize("code", LAZY_READS)
def test_a_module_grant_reaches_its_getattr(code):
    policy = Policy()
    policy.module(_lazy_module("lazymod"), name="lazymod", network_access=True)
    result = _exec(policy, code)
    assert result.error is None
    assert result.namespace["value"] is True


@pytest.mark.parametrize("code", LAZY_READS)
def test_a_module_getattr_without_a_grant_is_still_denied(code):
    policy = Policy()
    policy.module(_lazy_module("lazymod"), name="lazymod")
    result = _exec(policy, code)
    assert DENIED in str(result.error)


# -- host filesystem and host time -----------------------------------------------


# Two reads of Host.slow each fit the timeout; together they don't. The loop
# reaches a checkpoint after them, where the timeout is noticed.
TWO_READS = """\
a = h.slow
b = h.slow
for i in range(1000):
    pass
"""


class Host:
    data = "stored"

    @property
    def cwd(self) -> str:
        return os.getcwd()

    @property
    def slow(self) -> str:
        time.sleep(0.4)
        return "done"


def test_a_host_fs_grant_reaches_a_getter():
    policy = Policy()
    policy.cls(Host, host_fs_access=True)
    sandbox = Sandbox(policy, filesystem=VirtualFS({}))
    result = sandbox.exec("cwd = h.cwd", namespace={"h": Host()})
    assert result.error is None
    assert result.namespace["cwd"] == os.getcwd()


def test_a_host_time_getter_is_off_the_clock():
    policy = Policy(timeout=0.6)
    policy.cls(Host, host_time=True)
    result = _exec(policy, TWO_READS, h=Host())
    assert result.error is None
    assert result.namespace["b"] == "done"


def test_an_unmarked_getter_is_the_codes_time():
    policy = Policy(timeout=0.6)
    policy.cls(Host)
    result = _exec(policy, TWO_READS, h=Host())
    assert isinstance(result.error, StTimeout)


def test_a_read_that_runs_nothing_takes_no_grant():
    """A host_time registration pauses the clock for a getter, not for a
    stored value: under process isolation each pause is a message to the
    parent."""
    seen = []
    observe_pauses(lambda scope, paused: seen.append(paused))
    try:
        policy = Policy()
        policy.cls(Host, host_time=True)
        result = _exec(policy, "value = h.data", h=Host())
        assert result.error is None
        assert seen == []
        result = _exec(policy, "value = h.cwd", h=Host())
        assert result.error is None
        assert seen == [True, False]
    finally:
        observe_pauses(None)


# -- getters run once, and only when allowed ------------------------------------


class Counted:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def get(self) -> int:
        return 1

    @property
    def value(self) -> int:
        self.reads.append("value")
        return 42

    @property
    def hidden(self) -> int:
        self.reads.append("hidden")
        return 0


def test_a_getter_runs_once_per_read():
    policy = Policy()
    policy.cls(Counted)
    obj = Counted()
    result = _exec(policy, "v = c.value", c=obj)
    assert result.error is None
    assert obj.reads == ["value"]


@pytest.mark.parametrize("filters", [{"include": ["get"]}, {"exclude": ["hidden"]}])
def test_a_getter_the_policy_refuses_does_not_run(filters):
    policy = Policy()
    policy.cls(Counted, **filters)
    obj = Counted()
    result = _exec(policy, "v = c.hidden", c=obj)
    assert isinstance(result.error, AttributeError)
    assert obj.reads == []


def _counting_module(name: str, answers: dict) -> tuple[types.ModuleType, list]:
    """A module with ``fetch`` bound, and a ``__getattr__`` that records
    each name it is asked for, notes whether the network was open to it,
    and answers from *answers*."""
    mod = types.ModuleType(name)
    calls: list[tuple[str, bool]] = []

    def __getattr__(attr: str):
        try:
            _resolve()
            calls.append((attr, True))
        except Exception:
            calls.append((attr, False))
        if attr in answers:
            return answers[attr]
        raise AttributeError(attr)

    mod.fetch = lambda: "ok"
    mod.__getattr__ = __getattr__
    return mod, calls


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("import countmod\nv = countmod.secret", AttributeError),
        ("from countmod import secret", ImportError),
    ],
)
def test_a_module_name_the_policy_refuses_runs_nothing(code, error):
    """The include list leaves `secret` out, so the module's __getattr__ is
    not asked for it, with the module's grants or without them."""
    mod, calls = _counting_module("countmod", {"secret": "s"})
    policy = Policy()
    policy.module(mod, name="countmod", include=["fetch"], network_access=True)
    result = _exec(policy, code)
    assert isinstance(result.error, error)
    assert calls == []


@pytest.mark.parametrize(
    "code", ["import countmod\nv = countmod.lazy", "from countmod import lazy as v"]
)
def test_a_module_getattr_runs_once_per_read_under_its_grant(code):
    mod, calls = _counting_module("countmod", {"lazy": "value"})
    policy = Policy()
    policy.module(mod, name="countmod", network_access=True)
    result = _exec(policy, code)
    assert result.error is None
    assert result.namespace["v"] == "value"
    assert calls == [("lazy", True)]


def _lazy_package() -> tuple[types.ModuleType, types.ModuleType]:
    """A package that hands out its submodule only from __getattr__."""
    child = types.ModuleType("lazypkg.child")
    child.value = 1
    parent, _ = _counting_module("lazypkg", {"child": child})
    return parent, child


def test_a_non_recursive_module_refuses_a_submodule_its_getattr_hands_out():
    parent, _ = _lazy_package()
    policy = Policy()
    policy.module(parent, name="lazypkg")
    result = _exec(policy, "import lazypkg\nsub = lazypkg.child")
    assert isinstance(result.error, AttributeError)
    assert "child" in str(result.error)


def test_a_separately_registered_submodule_from_getattr_is_allowed():
    parent, child = _lazy_package()
    policy = Policy()
    policy.module(parent, name="lazypkg")
    policy.module(child, name="lazypkg.child")
    result = _exec(policy, "import lazypkg\nv = lazypkg.child.value")
    assert result.error is None
    assert result.namespace["v"] == 1


def test_a_name_outside_include_is_judged_by_what_the_module_binds():
    """Outside the include list, a name passes only as a separately
    registered object the module binds itself; one its __getattr__ would
    hand out is refused without asking it."""
    parent, child = _lazy_package()
    policy = Policy()
    policy.module(parent, name="lazypkg", include=["fetch"])
    policy.module(child, name="lazypkg.child")
    result = _exec(policy, "import lazypkg\nsub = lazypkg.child")
    assert isinstance(result.error, AttributeError)

    parent.child = child
    result = _exec(policy, "import lazypkg\nv = lazypkg.child.value")
    assert result.error is None
    assert result.namespace["v"] == 1


@pytest.fixture
def disk_package(tmp_path, monkeypatch):
    """An importable package, ``diskpkg``, whose ``child`` submodule is not
    imported by the package and, when its code runs, resolves a host
    name (a gated network call) and records that it ran on the package."""
    pkg = tmp_path / "diskpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("ran = []\n")
    (pkg / "child.py").write_text(
        "import socket\n"
        "import diskpkg\n"
        "socket.getaddrinfo('127.0.0.1', 80, type=socket.SOCK_STREAM)\n"
        "diskpkg.ran.append('child')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("diskpkg", "diskpkg.child"):
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    package = importlib.import_module("diskpkg")
    yield package
    for name in ("diskpkg", "diskpkg.child"):
        sys.modules.pop(name, None)


def test_a_submodule_the_policy_refuses_is_never_imported(disk_package):
    policy = Policy()
    policy.module(disk_package, name="diskpkg", network_access=True)
    result = _exec(policy, "from diskpkg import child")
    assert isinstance(result.error, ImportError)
    assert "is not allowed" in str(result.error)
    assert disk_package.ran == []
    assert "diskpkg.child" not in sys.modules


def test_a_submodule_the_policy_allows_imports_under_the_grant(disk_package):
    policy = Policy()
    policy.module(disk_package, name="diskpkg", network_access=True, recursive=True)
    result = _exec(policy, "from diskpkg import child")
    assert result.error is None
    assert disk_package.ran == ["child"]


def test_a_missing_member_still_reads_as_missing(disk_package):
    policy = Policy()
    policy.module(disk_package, name="diskpkg")
    result = _exec(policy, "from diskpkg import nothing")
    assert isinstance(result.error, ImportError)
    assert "has no attribute 'nothing'" in str(result.error)


# -- what counts as running code ---------------------------------------------------


class Shapes:
    __slots__ = ("slot",)
    plain = 1

    def __init__(self) -> None:
        self.slot = 2

    def method(self) -> None:
        pass

    @staticmethod
    def static() -> None:
        pass

    @classmethod
    def klass(cls) -> None:
        pass

    @property
    def prop(self) -> int:
        return 3


class Hooked:
    def __getattribute__(self, name: str):
        return object.__getattribute__(self, name)


class Fallback:
    def __getattr__(self, name: str):
        return name


_module = types.ModuleType("shapes_mod")
_module.value = 1
_module.fn = len


@pytest.mark.parametrize(
    ("obj", "attr", "runs"),
    [
        (Shapes(), "slot", False),
        (Shapes(), "plain", False),
        (Shapes(), "method", False),
        (Shapes(), "static", False),
        (Shapes(), "klass", False),
        (Shapes, "klass", False),
        (Shapes(), "prop", True),
        (Shapes(), "missing", True),
        (Hooked(), "anything", True),
        (Fallback(), "anything", True),
        (_module, "value", False),
        (_module, "fn", False),
        (_module, "missing", True),
    ],
)
def test_resolution_runs_code(obj, attr, runs):
    assert _resolution_runs_code(obj, attr) is runs
