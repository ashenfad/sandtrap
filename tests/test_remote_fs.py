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


class _CountingFS:
    """A filesystem that records every read asked of it.

    Wraps a real one and forwards everything else untouched, so a test
    can see the ranges the bridge actually requested rather than the
    bytes a worker ended up with.
    """

    def __init__(self, fs):
        self._fs = fs
        self.reads: list[tuple[str, int, int]] = []

    def read(self, path, offset=0, size=-1):
        self.reads.append((path, offset, size))
        return self._fs.read(path, offset, size)

    def __getattr__(self, name):
        return getattr(self._fs, name)


def test_a_ranged_read_crosses_the_boundary_as_a_range():
    """A worker asking for 50 bytes in the middle of a megabyte must
    cost the parent one 50-byte read, not a whole file it then slices."""
    import monkeyfs

    payload = bytes(range(256)) * 4096  # 1 MiB
    fs = _CountingFS(VirtualFS({}))
    fs.write("/blob.bin", payload)
    fs.reads.clear()

    policy = Policy(timeout=15.0)
    policy.module(monkeyfs, recursive=True)
    with sandbox(policy, isolation="process", filesystem=fs) as sb:
        r = sb.exec(
            "from monkeyfs import current_fs\n"
            "chunk = current_fs.get().read('/blob.bin', 500_000, 50)\n"
        )
    assert r.error is None, r.error
    assert r.namespace["chunk"] == payload[500_000:500_050]
    assert fs.reads == [("/blob.bin", 500_000, 50)]


def test_a_binary_read_is_lazy_across_the_boundary():
    """Seeking to the end of a megabyte and reading 100 bytes must cost
    one block, not the file: the whole point of the bridge forwarding a
    range is that the reader's access pattern is what the parent sees."""
    payload = bytes(range(256)) * 4096  # 1 MiB
    fs = _CountingFS(VirtualFS({}))
    fs.write("/blob.bin", payload)
    fs.reads.clear()

    with sandbox(Policy(timeout=15.0), isolation="process", filesystem=fs) as sb:
        r = sb.exec(
            "with open('/blob.bin', 'rb') as f:\n"
            "    f.seek(-100, 2)\n"
            "    tail = f.read(100)\n"
        )
    assert r.error is None, r.error
    assert r.namespace["tail"] == payload[-100:]
    assert len(fs.reads) == 1, fs.reads
    path, _offset, size = fs.reads[0]
    assert path == "/blob.bin"
    assert size <= 64 * 1024 < len(payload)


def test_a_whole_binary_read_still_returns_the_whole_file():
    """Laziness must not cost the common case a second call: a reader
    that wants everything gets everything, in one range."""
    payload = bytes(range(256)) * 4096  # 1 MiB
    fs = _CountingFS(VirtualFS({}))
    fs.write("/blob.bin", payload)
    fs.reads.clear()

    with sandbox(Policy(timeout=15.0), isolation="process", filesystem=fs) as sb:
        r = sb.exec("data = open('/blob.bin', 'rb').read()")
    assert r.error is None, r.error
    assert r.namespace["data"] == payload
    assert fs.reads == [("/blob.bin", 0, len(payload))]


def test_readline_stitches_a_line_across_a_block_boundary():
    """A line that straddles the 64 KiB block the reader is inside has
    to be joined from both blocks, not truncated at the seam."""
    first = b"x" * 65_500 + b"\n"
    second = b"y" * 100 + b"\n"
    fs = VirtualFS({})
    fs.write("/lines.bin", first + second + b"tail\n")

    with sandbox(Policy(timeout=15.0), isolation="process", filesystem=fs) as sb:
        r = sb.exec(
            "with open('/lines.bin', 'rb') as f:\n"
            "    one = f.readline()\n"
            "    two = f.readline()\n"
            "    three = f.readline()\n"
            "    rest = f.readline()\n"
        )
    assert r.error is None, r.error
    assert r.namespace["one"] == first
    assert r.namespace["two"] == second
    assert r.namespace["three"] == b"tail\n"
    assert r.namespace["rest"] == b""


def test_a_read_only_filesystem_still_serves_a_lazy_binary_read():
    """Reading is what a read-only filesystem is for; making the read
    lazy must not turn it into something the wrapper refuses."""
    from monkeyfs import ReadOnlyFS

    payload = bytes(range(256)) * 4096  # 1 MiB
    inner = VirtualFS({})
    inner.write("/blob.bin", payload)
    fs = _CountingFS(ReadOnlyFS(inner))

    with sandbox(Policy(timeout=15.0), isolation="process", filesystem=fs) as sb:
        r = sb.exec("with open('/blob.bin', 'rb') as f:\n    head = f.read(16)\n")
        refused = sb.exec("open('/blob.bin', 'wb').write(b'nope')")
    assert r.error is None, r.error
    assert r.namespace["head"] == payload[:16]
    assert len(fs.reads) == 1 and fs.reads[0][2] <= 64 * 1024
    assert isinstance(refused.error, PermissionError)
