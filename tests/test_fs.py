"""Tests for filesystem interception (Phase 8)."""

import os
import pathlib

import pytest

from sandtrap import MemoryFS, Policy, Sandbox
from sandtrap.fs import current_fs, install as install_fs, suspend_fs_interception, use_fs


@pytest.fixture(autouse=True)
def _install_fs_patches():
    """Ensure filesystem patches are installed for all tests."""
    install_fs()


def test_fs_routes_to_vfs():
    """Sandbox file operations route through the provided filesystem."""
    memfs = MemoryFS()
    memfs.files["/data.txt"] = "hello world"

    policy = Policy()
    sandbox = Sandbox(policy, filesystem=memfs)
    result = sandbox.exec("""\
f = open('/data.txt', 'r')
content = f.read()
f.close()
""")
    assert result.error is None
    assert result.namespace["content"] == "hello world"


def test_fs_write_routes_to_vfs():
    """File writes go through the VFS."""
    memfs = MemoryFS()

    policy = Policy()
    sandbox = Sandbox(policy, filesystem=memfs)
    result = sandbox.exec("""\
f = open('/output.txt', 'w')
f.write('test data')
f.close()
""")
    assert result.error is None
    assert memfs.files["/output.txt"] == "test data"


def test_fs_exists_routes_to_vfs():
    """os.path.exists checks against the VFS."""
    memfs = MemoryFS()
    memfs.files["/present.txt"] = "data"

    policy = Policy()
    policy.module(os, recursive=True)
    sandbox = Sandbox(policy, filesystem=memfs)
    result = sandbox.exec("""\
import os
exists = os.path.exists('/present.txt')
missing = os.path.exists('/nope.txt')
""")
    assert result.error is None
    assert result.namespace["exists"] is True
    assert result.namespace["missing"] is False


def test_fs_listdir_routes_to_vfs():
    """os.listdir lists from the VFS."""
    memfs = MemoryFS()
    memfs.files["/a.txt"] = "a"
    memfs.files["/b.txt"] = "b"

    policy = Policy()
    policy.module(os)
    sandbox = Sandbox(policy, filesystem=memfs)
    result = sandbox.exec("""\
import os
entries = os.listdir('/')
""")
    assert result.error is None
    assert sorted(result.namespace["entries"]) == ["a.txt", "b.txt"]


def test_host_fs_access_suspends_interception():
    """Registered function with host_fs_access=True can access real filesystem."""
    memfs = MemoryFS()

    # This function accesses the real filesystem
    def get_real_cwd():
        return os.getcwd()

    policy = Policy()
    policy.fn(get_real_cwd, host_fs_access=True)
    sandbox = Sandbox(policy, filesystem=memfs)

    result = sandbox.exec("cwd = get_real_cwd()")
    assert result.error is None
    # Should return the real cwd, not the VFS cwd
    assert result.namespace["cwd"] == os.getcwd()


def test_context_var_isolation():
    """use_fs/suspend_fs_interception properly restore state."""
    memfs = MemoryFS()
    assert current_fs.get() is None
    with use_fs(memfs):
        assert current_fs.get() is memfs
        with suspend_fs_interception():
            assert current_fs.get() is None
        assert current_fs.get() is memfs
    assert current_fs.get() is None


def test_no_filesystem_uses_real_fs():
    """When no filesystem is provided, real OS functions are used."""
    policy = Policy()
    policy.module(os)
    sandbox = Sandbox(policy)
    result = sandbox.exec("""\
import os
cwd = os.getcwd()
""")
    assert result.error is None
    assert result.namespace["cwd"] == os.getcwd()


def test_fs_install_idempotent():
    """install() is idempotent -- calling it twice is safe."""
    from monkeyfs import patching

    patching.install()
    assert patching._installed

    # Second call is a no-op
    patching.install()
    assert patching._installed


def test_memoryfs_path_normalization():
    """MemoryFS normalizes .. and . in paths."""
    fs = MemoryFS()
    fs.files["/data/file.txt"] = "hello"

    sandbox = Sandbox(Policy(), filesystem=fs)
    result = sandbox.exec("""\
f = open('/data/../data/./file.txt')
content = f.read()
f.close()
""")
    assert result.error is None
    assert result.namespace["content"] == "hello"


def test_memoryfs_append_mode():
    """MemoryFS supports append mode."""
    fs = MemoryFS()
    fs.files["/log.txt"] = "line1\n"

    sandbox = Sandbox(Policy(), filesystem=fs)
    result = sandbox.exec("""\
f = open('/log.txt', 'a')
f.write('line2\\n')
f.close()
f = open('/log.txt')
content = f.read()
f.close()
""")
    assert result.error is None
    assert result.namespace["content"] == "line1\nline2\n"


def test_memoryfs_append_new_file():
    """MemoryFS append mode creates file if it doesn't exist."""
    fs = MemoryFS()
    sandbox = Sandbox(Policy(), filesystem=fs)
    result = sandbox.exec("""\
f = open('/new.txt', 'a')
f.write('hello')
f.close()
f = open('/new.txt')
content = f.read()
f.close()
""")
    assert result.error is None
    assert result.namespace["content"] == "hello"


def test_memoryfs_mkdir_parents():
    """mkdir(parents=True) creates intermediate directories."""
    fs = MemoryFS()
    fs.mkdir("/a/b/c", parents=True)
    assert "/a" in fs.dirs
    assert "/a/b" in fs.dirs
    assert "/a/b/c" in fs.dirs


def test_memoryfs_mkdir_no_parents_fails():
    """mkdir without parents fails when parent doesn't exist."""
    fs = MemoryFS()
    try:
        fs.mkdir("/a/b")
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError:
        pass


def test_memoryfs_rename_directory():
    """rename() moves a directory and its contents."""
    fs = MemoryFS()
    fs.mkdir("/src", parents=True)
    fs.mkdir("/src/sub", parents=True)
    fs.files["/src/a.txt"] = "a"
    fs.files["/src/sub/b.txt"] = "b"

    fs.rename("/src", "/dst")

    assert "/src" not in fs.dirs
    assert "/dst" in fs.dirs
    assert "/dst/sub" in fs.dirs
    assert fs.files.get("/dst/a.txt") == "a"
    assert fs.files.get("/dst/sub/b.txt") == "b"
    assert "/src/a.txt" not in fs.files


def test_memoryfs_open_write_validates_parent():
    """MemoryFS.open('w') raises if parent directory doesn't exist."""
    fs = MemoryFS()
    try:
        fs.open("/nonexistent/file.txt", "w")
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError:
        pass


# --- pathlib interception ---


def _pathlib_sandbox():
    """Create a sandbox with pathlib registered and a populated VFS."""
    fs = MemoryFS()
    fs.files["/data.txt"] = "hello pathlib"
    fs.files["/sub/nested.txt"] = "nested"
    fs.dirs.add("/sub")
    policy = Policy()
    policy.module(pathlib)
    return Sandbox(policy, filesystem=fs), fs


def test_pathlib_read_text():
    """Path.read_text() should read from the VFS."""
    sandbox, _ = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
content = pathlib.Path('/data.txt').read_text()
""")
    assert result.error is None
    assert result.namespace["content"] == "hello pathlib"


def test_pathlib_write_text():
    """Path.write_text() should write to the VFS."""
    sandbox, fs = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
pathlib.Path('/output.txt').write_text('written via pathlib')
""")
    assert result.error is None
    assert fs.files["/output.txt"] == "written via pathlib"


def test_pathlib_exists():
    """Path.exists() should check the VFS."""
    sandbox, _ = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
present = pathlib.Path('/data.txt').exists()
missing = pathlib.Path('/nope.txt').exists()
""")
    assert result.error is None
    assert result.namespace["present"] is True
    assert result.namespace["missing"] is False


def test_pathlib_is_file():
    """Path.is_file() should check the VFS."""
    sandbox, _ = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
is_file = pathlib.Path('/data.txt').is_file()
is_dir = pathlib.Path('/sub').is_file()
""")
    assert result.error is None
    assert result.namespace["is_file"] is True
    assert result.namespace["is_dir"] is False


def test_pathlib_is_dir():
    """Path.is_dir() should check the VFS."""
    sandbox, _ = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
is_dir = pathlib.Path('/sub').is_dir()
is_file = pathlib.Path('/data.txt').is_dir()
""")
    assert result.error is None
    assert result.namespace["is_dir"] is True
    assert result.namespace["is_file"] is False


def test_pathlib_iterdir():
    """Path.iterdir() should list VFS contents."""
    sandbox, _ = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
names = sorted(p.name for p in pathlib.Path('/').iterdir())
""")
    assert result.error is None
    assert "data.txt" in result.namespace["names"]


def test_pathlib_mkdir():
    """Path.mkdir() should create directories in the VFS."""
    sandbox, fs = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
pathlib.Path('/newdir').mkdir()
""")
    assert result.error is None
    assert "/newdir" in fs.dirs


def test_pathlib_unlink():
    """Path.unlink() should remove files from the VFS."""
    sandbox, fs = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
pathlib.Path('/data.txt').unlink()
""")
    assert result.error is None
    assert "/data.txt" not in fs.files


def test_pathlib_rename():
    """Path.rename() should rename files in the VFS."""
    sandbox, fs = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
pathlib.Path('/data.txt').rename('/moved.txt')
""")
    assert result.error is None
    assert "/data.txt" not in fs.files
    assert fs.files["/moved.txt"] == "hello pathlib"


def test_pathlib_open_read():
    """Path.open() should read from the VFS."""
    sandbox, _ = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
with pathlib.Path('/data.txt').open() as f:
    content = f.read()
""")
    assert result.error is None
    assert result.namespace["content"] == "hello pathlib"


def test_pathlib_open_write():
    """Path.open('w') should write to the VFS."""
    sandbox, fs = _pathlib_sandbox()
    result = sandbox.exec("""\
import pathlib
with pathlib.Path('/written.txt').open('w') as f:
    f.write('via open')
""")
    assert result.error is None
    assert fs.files["/written.txt"] == "via open"
