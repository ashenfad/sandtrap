"""RemoteFS: in-memory filesystems bridged over the RPC channel.

The contract under test: with ``isolation="process"`` and a
``VirtualFS``, the PARENT's filesystem instance is the single source of
truth — worker writes land in it, parent seeds are readable, metadata
ops round-trip. (Fork inheritance previously handed the worker a
divergent copy whose writes vanished.)
"""

import pytest
from monkeyfs import VirtualFS

from sandtrap import Policy, sandbox


@pytest.fixture
def fs():
    return VirtualFS({})


@pytest.fixture
def sb(fs):
    import os as os_module

    policy = Policy(timeout=15.0)
    # grant os (recursive: os.path too) so sandboxed code can exercise
    # the patched metadata ops — makedirs/listdir/chdir/os.path route
    # through the fs interception layer
    policy.module(os_module, recursive=True)
    with sandbox(policy, isolation="process", filesystem=fs) as s:
        yield s


def test_worker_write_lands_in_parent(fs, sb):
    r = sb.exec("open('/out.txt', 'w').write('hello parent')")
    assert r.error is None
    assert fs.read("/out.txt") == b"hello parent"


def test_parent_seed_readable_in_worker(fs, sb):
    fs.write("/seed.txt", b"from parent")
    r = sb.exec("content = open('/seed.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "from parent"


def test_binary_roundtrip(fs, sb):
    payload = bytes(range(256)) * 4096  # 1MB
    fs.write("/blob.bin", payload)
    r = sb.exec(
        "data = open('/blob.bin', 'rb').read()\n"
        "open('/copy.bin', 'wb').write(data[::-1])"
    )
    assert r.error is None
    assert fs.read("/copy.bin") == payload[::-1]


def test_append_extends_parent_content(fs, sb):
    fs.write("/log.txt", b"line1\n")
    r = sb.exec("open('/log.txt', 'a').write('line2\\n')")
    assert r.error is None
    assert fs.read("/log.txt") == b"line1\nline2\n"


def test_context_manager_and_iteration(fs, sb):
    fs.write("/data.txt", b"a\nb\nc\n")
    r = sb.exec(
        "with open('/data.txt') as f:\n    lines = [line.strip() for line in f]"
    )
    assert r.error is None
    assert r.namespace["lines"] == ["a", "b", "c"]


def test_missing_file_raises_in_worker(sb):
    r = sb.exec("open('/nope.txt').read()")
    assert isinstance(r.error, FileNotFoundError)


def test_exclusive_create(fs, sb):
    fs.write("/taken.txt", b"x")
    r = sb.exec("open('/taken.txt', 'x')")
    assert isinstance(r.error, FileExistsError)
    r = sb.exec("open('/fresh.txt', 'x').write('new')")
    assert r.error is None
    assert fs.read("/fresh.txt") == b"new"


def test_mode_gating(fs, sb):
    fs.write("/ro.txt", b"data")
    r = sb.exec("open('/ro.txt', 'r').write('nope')")
    assert r.error is not None  # not writable
    r = sb.exec("open('/wo.txt', 'w').read()")
    assert r.error is not None  # not readable


def test_metadata_ops_roundtrip(fs, sb):
    r = sb.exec(
        "import os\n"
        "os.makedirs('/a/b', exist_ok=True)\n"
        "open('/a/b/f.txt', 'w').write('x')\n"
        "listing = sorted(os.listdir('/a/b'))\n"
        "there = os.path.exists('/a/b/f.txt')\n"
        "isdir = os.path.isdir('/a/b')"
    )
    assert r.error is None, r.error
    assert r.namespace["listing"] == ["f.txt"]
    assert r.namespace["there"] is True
    assert r.namespace["isdir"] is True
    # and the parent agrees
    assert fs.exists("/a/b/f.txt")
    assert fs.isdir("/a/b")


def test_cwd_is_shared_state(fs, sb):
    fs.makedirs("/work", exist_ok=True)
    r = sb.exec("import os\nos.chdir('/work')\nopen('rel.txt', 'w').write('rel')")
    assert r.error is None, r.error
    assert fs.exists("/work/rel.txt")
    # the parent fs's cwd moved too — it IS the same filesystem
    assert fs.getcwd() == "/work"


def test_writes_survive_worker_crash_respawn(fs):
    """State lives in the parent: a worker crash loses nothing already
    written, and the respawned worker sees it."""
    import os as host_os
    import signal

    from sandtrap.process.sandbox import ProcessSandbox

    with ProcessSandbox(Policy(timeout=15.0), filesystem=fs) as ps:
        assert ps.exec("open('/kept.txt', 'w').write('before crash')").error is None
        host_os.kill(ps._process.pid, signal.SIGKILL)
        ps._process.join(timeout=5.0)
        r = ps.exec("content = open('/kept.txt').read()")
        assert r.error is None
        assert r.namespace["content"] == "before crash"
    assert fs.read("/kept.txt") == b"before crash"


def test_pathlike_metadata_crosses_the_boundary(fs, sb):
    """The rest of the monkeyfs surface: os.path.realpath (matplotlib's
    savefig calls it), getsize, samefile — all previously raised
    'RemoteFS does not implement ...' and broke plain library code
    under process isolation."""
    fs.write("/plot.png", b"\x89PNG fake")
    r = sb.exec(
        "import os\n"
        "rp = os.path.realpath('/plot.png')\n"
        "size = os.path.getsize('/plot.png')\n"
        "same = os.path.samefile('/plot.png', '/plot.png')\n"
        "os.makedirs('/tmp2', exist_ok=True)\n"
        "os.rmdir('/tmp2')\n"
        "gone = not os.path.exists('/tmp2')"
    )
    assert r.error is None, r.error
    assert r.namespace["rp"] == "/plot.png"
    assert r.namespace["size"] == len(b"\x89PNG fake")
    assert r.namespace["same"] is True
    assert r.namespace["gone"] is True


def test_replace_crosses_the_boundary(fs, sb):
    fs.write("/old.txt", b"content")
    r = sb.exec("import os\nos.replace('/old.txt', '/new.txt')")
    assert r.error is None, r.error
    assert fs.exists("/new.txt") and not fs.exists("/old.txt")
