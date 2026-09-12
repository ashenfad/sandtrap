"""Tests for per-exec modules (``exec(..., modules={...})``).

The embedder hands code a module for one execution the way it hands it a
namespace: the module exists for that call and is gone when the call is.
"""

from __future__ import annotations

import math

import pytest

from sandtrap import IsolationUnavailable, Policy, RpcProxyMarker, VirtualFS, sandbox

ISOLATIONS = ("none", "process", "kernel")
WORKER_ISOLATIONS = ("process", "kernel")


def _sandbox(isolation, *, policy=None, files=None, rpc_handlers=None):
    """A sandbox at *isolation*, with a VirtualFS when *files* is given."""
    fs = None
    if files is not None:
        fs = VirtualFS({})
        for path, source in files.items():
            fs.write(path, source.encode())
    try:
        return sandbox(
            policy or Policy(timeout=30.0),
            isolation=isolation,
            filesystem=fs,
            rpc_handlers=rpc_handlers,
        )
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


# ------------------------------------------------------------------
# Resolving a per-exec module
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_import_binds_the_module_at_the_top_level(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host\nname = host.NAME\nn = host.count\n",
            modules={"host": {"NAME": "acme", "count": 3}},
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["name"] == "acme"
    assert result.namespace["n"] == 3


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_from_import_pulls_an_attribute(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "from host import db\nwhich = db\n",
            modules={"host": {"db": "postgres"}},
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["which"] == "postgres"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_import_as_binds_the_alias(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host as h\nwhich = h.db\n",
            modules={"host": {"db": "postgres"}},
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["which"] == "postgres"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_vfs_module_sees_the_same_modules(isolation):
    """Workspace modules share the exec's import gates."""
    files = {
        "/report.py": (
            "import host\nfrom host import db\n\n"
            "def describe():\n"
            "    return f'{host.NAME}/{db}'\n"
        )
    }
    with _sandbox(isolation, files=files) as sb:
        result = sb.exec(
            "from report import describe\nline = describe()\n",
            modules={"host": {"NAME": "acme", "db": "postgres"}},
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["line"] == "acme/postgres"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_underscore_attribute_is_readable(isolation):
    """The embedder chose the contents, so the default exclude does not apply."""
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host\nsecret = host._token\n",
            modules={"host": {"_token": "abc123"}},
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["secret"] == "abc123"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_plain_data_crosses_by_value(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host\ntotal = sum(host.rows) + host.config['offset']\n",
            modules={"host": {"rows": [1, 2, 3], "config": {"offset": 10}}},
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["total"] == 16


# ------------------------------------------------------------------
# The module is gone when the exec is
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_without_modules_the_import_fails(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec("import host")
    assert isinstance(result.error, ImportError), f"resolved: {result.error!r}"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_consecutive_execs_see_only_their_own_modules(isolation):
    """A pooled worker never carries a per-exec module into the next exec."""
    with _sandbox(isolation) as sb:
        first = sb.exec(
            "import host\nseen = host.which\n", modules={"host": {"which": "one"}}
        )
        second = sb.exec(
            "import host\nseen = host.which\n", modules={"host": {"which": "two"}}
        )
        third = sb.exec("import host")

    assert first.error is None, f"unexpected error: {first.error}"
    assert first.namespace["seen"] == "one"
    assert second.error is None, f"unexpected error: {second.error}"
    assert second.namespace["seen"] == "two"
    assert isinstance(third.error, ImportError), f"leaked: {third.error!r}"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_the_module_is_not_in_the_result_namespace(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec("import host\nx = 1", modules={"host": {"db": "postgres"}})
    assert result.error is None, f"unexpected error: {result.error}"
    assert "host" not in result.namespace


# ------------------------------------------------------------------
# Read-only from sandboxed code
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_assignment_to_a_module_attribute_is_refused(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host\nhost.db = 'mine'\n", modules={"host": {"db": "postgres"}}
        )
    assert isinstance(result.error, AttributeError), f"allowed: {result.error!r}"
    assert "host" in str(result.error)


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_new_module_attribute_cannot_be_created(isolation):
    """A per-exec module must not become a channel between execs."""
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host\nhost.smuggled = 1\n", modules={"host": {"db": "postgres"}}
        )
    assert isinstance(result.error, AttributeError), f"allowed: {result.error!r}"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_deleting_a_module_attribute_is_refused(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            "import host\ndel host.db\n", modules={"host": {"db": "postgres"}}
        )
    assert isinstance(result.error, AttributeError), f"allowed: {result.error!r}"


# ------------------------------------------------------------------
# Rejected names
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_name_a_granted_module_already_owns_is_refused(isolation):
    policy = Policy(timeout=30.0)
    policy.module(math)
    with _sandbox(isolation, policy=policy) as sb:
        with pytest.raises(ValueError, match="math"):
            sb.exec("import math", modules={"math": {"pi": 3}})


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_the_name_sys_is_refused(isolation):
    with _sandbox(isolation) as sb:
        with pytest.raises(ValueError, match="sys"):
            sb.exec("import sys", modules={"sys": {"argv": []}})


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_a_dotted_name_is_refused(isolation):
    with _sandbox(isolation) as sb:
        with pytest.raises(ValueError, match="host.db"):
            sb.exec("x = 1", modules={"host.db": {"url": "x"}})


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_from_import_of_a_missing_name_names_the_module(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec("from host import missing", modules={"host": {"db": "pg"}})
    assert isinstance(result.error, ImportError), f"resolved: {result.error!r}"
    assert "missing" in str(result.error)
    assert "host" in str(result.error)


# ------------------------------------------------------------------
# Live host objects under worker isolation
# ------------------------------------------------------------------


class _Counter:
    """Parent-side object reached through an RPC proxy."""

    def __init__(self) -> None:
        self.n = 0

    def value(self) -> int:
        return self.n


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_a_live_host_object_arrives_as_a_working_proxy(isolation):
    """The module's attribute values are materialized the way namespace
    entries are, so a marker becomes a proxy onto the parent's object."""
    counter = _Counter()

    def handler(method, args, kwargs):
        return getattr(counter, method)(*args, **kwargs)

    marker = RpcProxyMarker(target="counter", methods=("value",))
    with _sandbox(isolation, rpc_handlers={"counter": handler}) as sb:
        before = sb.exec(
            "import host\nseen = host.obj.value()\n", modules={"host": {"obj": marker}}
        )
        counter.n = 5
        after = sb.exec(
            "import host\nseen = host.obj.value()\n", modules={"host": {"obj": marker}}
        )

    assert before.error is None, f"unexpected error: {before.error}"
    assert before.namespace["seen"] == 0
    assert after.error is None, f"unexpected error: {after.error}"
    assert after.namespace["seen"] == 5


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_an_unpicklable_attribute_is_dropped_with_a_warning(isolation):
    """Module attributes cross through the namespace's picklability filter."""
    with _sandbox(isolation) as sb:
        with pytest.warns(RuntimeWarning, match="host.fn"):
            result = sb.exec(
                "import host\nkept = host.ok\n",
                modules={"host": {"ok": 1, "fn": lambda: None}},
            )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["kept"] == 1


# ------------------------------------------------------------------
# aexec
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_aexec_takes_modules_too(isolation):
    """Every isolation level shares one API."""
    import asyncio

    with _sandbox(isolation) as sb:
        result = asyncio.run(
            sb.aexec("import host\nwhich = host.db\n", modules={"host": {"db": "pg"}})
        )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.namespace["which"] == "pg"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_an_absent_attribute_names_the_module(isolation):
    """The surface is the mapping, so anything else is simply not there."""
    with _sandbox(isolation) as sb:
        result = sb.exec("import host\nx = host.nope", modules={"host": {"db": "pg"}})
    assert isinstance(result.error, AttributeError), f"resolved: {result.error!r}"
    assert "host" in str(result.error)
    assert "nope" in str(result.error)


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_module_type_machinery_is_not_readable(isolation):
    """A per-exec module's surface is the mapping, not the module type."""
    with _sandbox(isolation) as sb:
        for expr in (
            "host.__dict__",
            "host.__getattribute__",
            "host.__class__",
            "getattr(host, '__dict__')",
        ):
            result = sb.exec(f"import host\nx = {expr}", modules={"host": {"db": "pg"}})
            assert isinstance(result.error, AttributeError), (
                f"{expr} leaked: {result.error!r}"
            )


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_from_import_of_module_type_machinery_is_refused(isolation):
    """The import gate refuses what the attribute gate refuses."""
    with _sandbox(isolation) as sb:
        for name in ("__getattribute__", "__dict__", "__class__"):
            result = sb.exec(
                f"from host import {name} as x", modules={"host": {"db": "pg"}}
            )
            assert isinstance(result.error, ImportError), (
                f"{name} leaked: {result.error!r}"
            )
