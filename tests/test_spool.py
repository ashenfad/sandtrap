"""Tests for spooled results (``ProcessSandbox(spool=...)``).

The worker writes each result value to a file of its own and sends names
and sizes; the parent reads a value when it is first asked for.
"""

from __future__ import annotations

import asyncio
import gc
import os

import pytest

from sandtrap import IsolatedFS, IsolationUnavailable, Policy, sandbox
from sandtrap.process.spool import (
    INLINE_LIMIT,
    SpoolClosedError,
    SpooledNamespace,
    SpoolEntry,
)

WORKER_ISOLATIONS = ("process", "kernel")
BIG = INLINE_LIMIT * 4


def _sandbox(isolation, *, spool=True, filesystem=None, **kwargs):
    try:
        return sandbox(
            Policy(timeout=30.0),
            isolation=isolation,
            spool=spool,
            filesystem=filesystem,
            **kwargs,
        )
    except IsolationUnavailable as e:
        pytest.skip(f"kernel isolation unavailable: {e}")


def _files(sb):
    return sorted(os.listdir(sb._spool_dir))


# ------------------------------------------------------------------
# What comes back
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_spooled_values_read_back_as_themselves(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            f"big = bytes(range(256)) * ({BIG} // 256)\n"
            "small = {'n': 3, 'xs': [1, 2]}\n"
            "text = 'hello'\n"
        )
        assert result.error is None, result.error
        assert result.namespace["small"] == {"n": 3, "xs": [1, 2]}
        assert result.namespace["text"] == "hello"
        assert result.namespace["big"] == bytes(range(256)) * (BIG // 256)
        assert dict(result.namespace) == {
            "big": bytes(range(256)) * (BIG // 256),
            "small": {"n": 3, "xs": [1, 2]},
            "text": "hello",
        }


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_sizes_are_known_before_anything_is_read(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(f"big = bytearray({BIG})\nn = 1\n")
        assert set(result.sizes) == {"big", "n"}
        assert result.sizes["big"] > BIG
        assert result.sizes["n"] < 100
        # only the large value made a file, and nothing has been read
        assert len(_files(sb)) == 1
        assert "read" not in repr(result.namespace)


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_in_and_iteration_read_nothing(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(f"big = bytearray({BIG})\n")
        assert "big" in result.namespace
        assert "other" not in result.namespace
        assert list(result.namespace) == ["big"]
        assert len(result.namespace) == 1
        assert len(_files(sb)) == 1


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_reading_a_value_removes_its_file_and_keeps_the_value(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(f"big = bytearray({BIG})\n")
        first = result.namespace["big"]
        assert _files(sb) == []
        assert result.namespace["big"] is first


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_letting_go_of_a_result_removes_unread_files(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(f"a = bytearray({BIG})\nb = bytearray({BIG})\n")
        assert len(_files(sb)) == 2
        del result
        gc.collect()
        assert _files(sb) == []


def test_a_missing_name_is_a_key_error():
    with _sandbox("process") as sb:
        result = sb.exec("a = 1\n")
        with pytest.raises(KeyError):
            result.namespace["b"]
        assert result.namespace.get("b") is None


# ------------------------------------------------------------------
# What can't come back
# ------------------------------------------------------------------


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_an_unpicklable_value_is_dropped_and_leaves_no_file(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            f"pad = bytearray({BIG})\n"
            # fails only after the bytearray before it was written
            f"bad = [bytearray({BIG}), lambda: 1]\n"
        )
        assert result.dropped == ("bad",)
        assert set(result.namespace) == {"pad"}
        assert len(_files(sb)) == 1


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_a_class_of_the_codes_own_is_refused(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            f"class Mine:\n    pass\npayload = [bytearray({BIG}), Mine()]\nok = 2\n"
        )
        assert set(result.dropped) == {"Mine", "payload"}
        assert result.namespace == {"ok": 2}
        assert _files(sb) == []


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_keep_spools_only_the_kept_names(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(f"scratch = bytearray({BIG})\nui = {{'n': 1}}\n", keep=("ui",))
        assert result.sizes.keys() == {"ui"}
        assert result.namespace == {"ui": {"n": 1}}
        assert _files(sb) == []


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_a_memory_error_spools_nothing(isolation):
    with _sandbox(isolation) as sb:
        result = sb.exec(
            f"scratch = bytearray({BIG})\nraise MemoryError('synthetic')\n"
        )
        assert isinstance(result.error, MemoryError)
        assert result.namespace == {}
        assert result.sizes == {}
        assert result.dropped == ("scratch",)
        assert _files(sb) == []


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


def test_closing_the_sandbox_removes_the_directory():
    with _sandbox("process") as sb:
        result = sb.exec(f"big = bytearray({BIG})\nn = 1\n")
        directory = sb._spool_dir
        assert os.path.isdir(directory)
    assert not os.path.exists(directory)
    # a value that rode the pipe is still there; a spooled one is gone
    assert result.namespace["n"] == 1
    with pytest.raises(SpoolClosedError, match="before the sandbox exits"):
        result.namespace["big"]


def test_the_directory_is_private_and_under_the_given_base(tmp_path):
    with _sandbox("process", spool=tmp_path) as sb:
        sb.exec("pass\n")
        directory = sb._spool_dir
        assert os.path.dirname(directory) == os.path.realpath(tmp_path)
        assert os.stat(directory).st_mode & 0o777 == 0o700


def test_spooling_survives_a_worker_crash():
    with _sandbox("process") as sb:
        crashed = sb.exec("import os\nos._exit(1)\n")
        assert crashed.error is not None
        result = sb.exec(f"big = bytearray({BIG})\n")
        assert result.error is None, result.error
        assert len(result.namespace["big"]) == BIG


def test_re_entering_a_sandbox_spools_again():
    sb = _sandbox("process")
    with sb:
        first = sb._spool_dir
    with sb:
        result = sb.exec(f"big = bytearray({BIG})\n")
        assert sb._spool_dir != first
        assert len(result.namespace["big"]) == BIG


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_aexec_spools_too(isolation):
    with _sandbox(isolation) as sb:
        result = asyncio.run(sb.aexec(f"big = bytearray({BIG})\n"))
        assert result.sizes.keys() == {"big"}
        assert len(result.namespace["big"]) == BIG


@pytest.mark.parametrize("isolation", WORKER_ISOLATIONS)
def test_without_spool_nothing_changes(isolation):
    with _sandbox(isolation, spool=False) as sb:
        result = sb.exec("n = 1\n")
        assert type(result.namespace) is dict
        assert result.sizes == {}
        assert sb._spool_dir is None


def test_in_process_ignores_spool():
    with sandbox(Policy(timeout=30.0), isolation="none", spool=True) as sb:
        result = sb.exec("n = 1\n")
    assert result.namespace == {"n": 1}
    assert result.sizes == {}


def test_spool_must_be_a_flag_or_a_directory():
    from sandtrap.process.sandbox import ProcessSandbox

    with pytest.raises(TypeError, match="spool must be"):
        ProcessSandbox(Policy(timeout=30.0), spool=3)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# Kernel isolation with a restricted filesystem
# ------------------------------------------------------------------


def test_a_restricted_worker_can_still_spool(tmp_path):
    """With an IsolatedFS root, kernel isolation confines the worker's
    writes to that root; the spool directory is kept writable too."""
    root = tmp_path / "root"
    root.mkdir()
    with _sandbox("kernel", filesystem=IsolatedFS(str(root)), spool=tmp_path) as sb:
        result = sb.exec(
            f"open('/note.txt', 'w').write('hi')\nbig = bytearray({BIG})\n"
        )
        assert result.error is None, result.error
        assert len(result.namespace["big"]) == BIG
    assert (root / "note.txt").read_text() == "hi"


# ------------------------------------------------------------------
# SpooledNamespace
# ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../escape.pkl", "/etc/passwd", "a.pkl", "x" * 36])
def test_only_a_file_the_worker_could_have_named_is_read(tmp_path, name):
    with pytest.raises(ValueError, match="unexpected spool file name"):
        SpooledNamespace(str(tmp_path), {"v": SpoolEntry(size=1, file=name)})


# ------------------------------------------------------------------
# Keeping the spool out of the sandbox's reach
# ------------------------------------------------------------------


@pytest.mark.parametrize("where", ["root", "inside"])
def test_a_spool_inside_the_sandbox_root_is_refused(tmp_path, where):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    base = root if where == "root" else root / "sub"
    with pytest.raises(ValueError, match="inside the sandbox's filesystem root"):
        sandbox(
            Policy(timeout=30.0),
            isolation="process",
            filesystem=IsolatedFS(str(root)),
            spool=base,
        )


def test_a_temp_directory_inside_the_sandbox_root_is_refused(tmp_path, monkeypatch):
    import tempfile

    root = tmp_path / "root"
    (root / "tmp").mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(root / "tmp"))
    with pytest.raises(ValueError, match="system temporary directory"):
        sandbox(
            Policy(timeout=30.0),
            isolation="process",
            filesystem=IsolatedFS(str(root)),
            spool=True,
        )


def test_a_spool_beside_the_sandbox_root_is_fine(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with _sandbox("process", filesystem=IsolatedFS(str(root)), spool=tmp_path) as sb:
        result = sb.exec(f"big = bytearray({BIG})\n")
        assert len(result.namespace["big"]) == BIG


def test_a_spool_file_replaced_by_a_link_is_not_read(tmp_path):
    import pickle

    decoy = tmp_path / "decoy.pkl"
    decoy.write_bytes(pickle.dumps("not what the worker wrote"))
    with _sandbox("process") as sb:
        result = sb.exec(f"big = bytearray({BIG})\n")
        (name,) = _files(sb)
        path = os.path.join(sb._spool_dir, name)
        os.unlink(path)
        os.symlink(decoy, path)
        with pytest.raises(ValueError, match="not one the worker wrote"):
            result.namespace["big"]


def test_a_spool_file_replaced_by_a_directory_is_not_read():
    with _sandbox("process") as sb:
        result = sb.exec(f"big = bytearray({BIG})\n")
        (name,) = _files(sb)
        path = os.path.join(sb._spool_dir, name)
        os.unlink(path)
        os.mkdir(path)
        with pytest.raises(ValueError, match="not a regular file"):
            result.namespace["big"]


# ------------------------------------------------------------------
# Streaming without a copy
# ------------------------------------------------------------------


def test_a_value_holding_the_module_prefix_still_streams_without_a_copy():
    """Bytes that merely contain a sandbox module's name send the value
    through the refusal pass as well; that pass must not copy a large
    buffer either."""
    import pickle
    import tracemalloc

    from sandtrap.home import PREFIX, dump_sendable

    if pickle.DEFAULT_PROTOCOL >= 5:
        pytest.skip("the default protocol already streams a bytearray")

    class Sink:
        def write(self, data):
            return len(memoryview(data))

    size = 32 << 20
    value = bytearray(size)
    value[: len(PREFIX)] = PREFIX.encode()
    tracemalloc.start()
    try:
        dump_sendable(value, Sink())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < size // 4, f"peak {peak >> 20}MB for a {size >> 20}MB buffer"
