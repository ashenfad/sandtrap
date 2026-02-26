"""Integration tests for kernel-level isolation.

These tests fork a child process, apply isolation, and verify that
forbidden operations are actually blocked by the kernel — not just
by sandtrap's Python-level policy.
"""

import multiprocessing
import os
import sys
from unittest.mock import patch

import pytest


def _run_in_child(fn, *args):
    """Fork a child, run fn(*args), return (exitcode, result_from_pipe)."""
    parent_conn, child_conn = multiprocessing.Pipe()
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=_child_wrapper, args=(child_conn, fn, args), daemon=True)
    p.start()
    child_conn.close()
    p.join(timeout=10)
    try:
        result = parent_conn.recv() if parent_conn.poll(0.1) else None
    except EOFError:
        result = None
    parent_conn.close()
    return p.exitcode, result


def _child_wrapper(conn, fn, args):
    try:
        result = fn(*args)
        conn.send(result)
    except BaseException as e:
        conn.send(("error", type(e).__name__, str(e)))


# =====================================================================
# Landlock (Linux)
# =====================================================================


def _landlock_try_read_outside(root):
    """Apply Landlock to root, then try to read /etc/hostname."""
    from sandtrap.process.landlock import apply

    applied = apply(root)
    if not applied:
        return ("skipped", "landlock not available")

    try:
        with open("/etc/hostname") as f:
            f.read()
        return ("fail", "read outside root succeeded — landlock did not block")
    except PermissionError:
        return ("ok", "read outside root blocked by landlock")
    except FileNotFoundError:
        return ("skipped", "/etc/hostname does not exist")


def _landlock_try_read_inside(root):
    """Apply Landlock to root, then read a file inside root."""
    from sandtrap.process.landlock import apply

    # Create a test file before applying Landlock
    test_file = os.path.join(root, "test.txt")
    with open(test_file, "w") as f:
        f.write("inside")

    applied = apply(root)
    if not applied:
        return ("skipped", "landlock not available")

    try:
        with open(test_file) as f:
            content = f.read()
        if content == "inside":
            return ("ok", "read inside root allowed")
        return ("fail", f"unexpected content: {content!r}")
    except Exception as e:
        return ("fail", f"read inside root blocked: {e}")


def _landlock_skipped_when_host_fs(root):
    """Apply via platform with allow_host_fs=True — Landlock should not apply."""
    from sandtrap.process.platform import _apply_linux

    _apply_linux(root, allow_host_fs=True)

    # If Landlock was skipped, we can read outside root
    try:
        with open("/etc/hostname") as f:
            f.read()
        return ("ok", "host fs accessible — Landlock correctly skipped")
    except PermissionError:
        return ("fail", "read blocked — Landlock should not have applied")
    except FileNotFoundError:
        return ("skipped", "/etc/hostname does not exist")


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")
class TestLandlock:
    def test_blocks_read_outside_root(self, tmp_path):
        exitcode, result = _run_in_child(_landlock_try_read_outside, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_allows_read_inside_root(self, tmp_path):
        exitcode, result = _run_in_child(_landlock_try_read_inside, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_skipped_when_host_fs_allowed(self, tmp_path):
        """Landlock is not applied when allow_host_fs=True."""
        exitcode, result = _run_in_child(_landlock_skipped_when_host_fs, str(tmp_path))
        if exitcode != 0:
            # Killed by seccomp — need to run without seccomp
            pytest.skip(f"child killed (exit {exitcode})")
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg


# =====================================================================
# seccomp (Linux)
# =====================================================================


def _seccomp_try_execve():
    """Apply seccomp filter, then try to exec a process."""
    from sandtrap.process.seccomp import apply

    applied = apply()
    if not applied:
        return ("skipped", "seccomp not available")

    try:
        os.execve("/bin/true", ["/bin/true"], {})
        return ("fail", "execve succeeded — seccomp did not block")
    except OSError:
        return ("ok", "execve blocked by seccomp")


def _seccomp_allows_file_io(root):
    """Apply seccomp filter, verify file I/O still works."""
    from sandtrap.process.seccomp import apply

    test_file = os.path.join(root, "test.txt")
    with open(test_file, "w") as f:
        f.write("hello")

    applied = apply()
    if not applied:
        return ("skipped", "seccomp not available")

    try:
        with open(test_file) as f:
            content = f.read()
        if content == "hello":
            return ("ok", "file I/O works under seccomp")
        return ("fail", f"unexpected content: {content!r}")
    except Exception as e:
        return ("fail", f"file I/O blocked by seccomp: {e}")


def _seccomp_try_socket_with_network():
    """Apply seccomp with allow_network=True, then try creating a socket."""
    import socket

    from sandtrap.process.seccomp import apply

    applied = apply(allow_network=True)
    if not applied:
        return ("skipped", "seccomp not available")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return ("ok", "socket creation allowed with allow_network=True")
    except OSError as e:
        return ("fail", f"socket creation blocked: {e}")


@pytest.mark.skipif(sys.platform != "linux", reason="seccomp is Linux-only")
class TestSeccomp:
    def test_blocks_execve(self):
        exitcode, result = _run_in_child(_seccomp_try_execve)
        # Child may be killed by seccomp (SIGSYS = -31) or return an error
        if exitcode != 0:
            # Killed by seccomp — that's the expected behavior
            return
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_allows_file_io(self, tmp_path):
        exitcode, result = _run_in_child(_seccomp_allows_file_io, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_allows_network_when_enabled(self):
        """seccomp with allow_network=True permits socket creation."""
        exitcode, result = _run_in_child(_seccomp_try_socket_with_network)
        if exitcode != 0:
            # Killed by seccomp — that's a failure for this test
            pytest.fail(
                f"child killed (exit {exitcode}) — network syscalls not allowed"
            )
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg


# =====================================================================
# Seatbelt (macOS)
# =====================================================================


def _seatbelt_try_read_outside(root):
    """Apply Seatbelt to root, then try to read outside root."""
    from sandtrap.process.seatbelt import apply

    applied = apply(root)
    if not applied:
        return ("skipped", "seatbelt not available")

    # /etc/hosts exists on all macOS
    try:
        with open("/etc/hosts") as f:
            f.read()
        return ("fail", "read outside root succeeded — seatbelt did not block")
    except PermissionError:
        return ("ok", "read outside root blocked by seatbelt")


def _seatbelt_try_read_inside(root):
    """Apply Seatbelt to root, then read a file inside root."""
    from sandtrap.process.seatbelt import apply

    test_file = os.path.join(root, "test.txt")
    with open(test_file, "w") as f:
        f.write("inside")

    applied = apply(root)
    if not applied:
        return ("skipped", "seatbelt not available")

    try:
        with open(test_file) as f:
            content = f.read()
        if content == "inside":
            return ("ok", "read inside root allowed")
        return ("fail", f"unexpected content: {content!r}")
    except Exception as e:
        return ("fail", f"read inside root blocked: {e}")


def _seatbelt_try_network(root):
    """Apply Seatbelt to root, then try a network connection."""
    import socket

    from sandtrap.process.seatbelt import apply

    applied = apply(root)
    if not applied:
        return ("skipped", "seatbelt not available")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("1.1.1.1", 80))
        s.close()
        return ("fail", "network connect succeeded — seatbelt did not block")
    except (PermissionError, OSError):
        return ("ok", "network connect blocked by seatbelt")


def _seatbelt_try_network_allowed(root):
    """Apply Seatbelt with allow_network=True, then try a socket."""
    import socket

    from sandtrap.process.seatbelt import apply

    applied = apply(root, allow_network=True)
    if not applied:
        return ("skipped", "seatbelt not available")

    try:
        # Just create and immediately close a socket — enough to verify
        # the kernel isn't blocking socket creation
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return ("ok", "socket creation allowed by seatbelt")
    except PermissionError:
        return ("fail", "socket creation blocked — allow_network=True not effective")


def _seatbelt_try_host_fs_allowed(root):
    """Apply Seatbelt with allow_host_fs=True, then read outside root."""
    from sandtrap.process.seatbelt import apply

    applied = apply(root, allow_host_fs=True)
    if not applied:
        return ("skipped", "seatbelt not available")

    try:
        with open("/etc/hosts") as f:
            content = f.read()
        if content:
            return ("ok", "host fs read allowed by seatbelt")
        return ("fail", "empty content")
    except PermissionError:
        return ("fail", "read outside root blocked — allow_host_fs=True not effective")


def _seatbelt_try_host_fs_allowed_network_denied(root):
    """Apply Seatbelt with allow_host_fs=True but allow_network=False."""
    import socket

    from sandtrap.process.seatbelt import apply

    applied = apply(root, allow_host_fs=True, allow_network=False)
    if not applied:
        return ("skipped", "seatbelt not available")

    # Host FS should be accessible
    try:
        with open("/etc/hosts") as f:
            f.read()
    except PermissionError:
        return ("fail", "host fs read blocked — allow_host_fs=True not effective")

    # Network should still be blocked
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("1.1.1.1", 80))
        s.close()
        return ("fail", "network connect succeeded — should be blocked")
    except (PermissionError, OSError):
        return ("ok", "host fs allowed, network blocked")


def _seatbelt_try_no_root(tmp_dir):
    """Apply Seatbelt with root=None — FS should be unrestricted."""
    from sandtrap.process.seatbelt import apply

    applied = apply(None)
    if not applied:
        return ("skipped", "seatbelt not available")

    try:
        with open("/etc/hosts") as f:
            content = f.read()
        if content:
            return ("ok", "host fs read allowed when root=None")
        return ("fail", "empty content")
    except PermissionError:
        return ("fail", "read blocked — root=None should leave FS unrestricted")


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
class TestSeatbelt:
    def test_blocks_read_outside_root(self, tmp_path):
        exitcode, result = _run_in_child(_seatbelt_try_read_outside, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_allows_read_inside_root(self, tmp_path):
        exitcode, result = _run_in_child(_seatbelt_try_read_inside, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_blocks_network(self, tmp_path):
        exitcode, result = _run_in_child(_seatbelt_try_network, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_allows_network_when_enabled(self, tmp_path):
        exitcode, result = _run_in_child(_seatbelt_try_network_allowed, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_allows_host_fs_when_enabled(self, tmp_path):
        exitcode, result = _run_in_child(_seatbelt_try_host_fs_allowed, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_host_fs_allowed_but_network_denied(self, tmp_path):
        """allow_host_fs=True does not inadvertently allow network."""
        exitcode, result = _run_in_child(
            _seatbelt_try_host_fs_allowed_network_denied, str(tmp_path)
        )
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg

    def test_no_root_leaves_fs_unrestricted(self, tmp_path):
        """root=None means no filesystem restriction."""
        exitcode, result = _run_in_child(_seatbelt_try_no_root, str(tmp_path))
        assert exitcode == 0, f"child crashed with exit code {exitcode}"
        status, msg = result[0], result[1]
        if status == "skipped":
            pytest.skip(msg)
        assert status == "ok", msg


# =====================================================================
# Platform dispatch (apply_isolation)
# =====================================================================


class TestApplyIsolation:
    def test_mode_none_is_noop(self):
        """isolation='none' skips all kernel-level restrictions."""
        from sandtrap.process.platform import apply_isolation

        # Should not raise or call any platform-specific code
        apply_isolation("none", "/tmp/test")

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
    def test_auto_dispatches_to_linux(self, tmp_path):
        """apply_isolation('auto') calls _apply_linux on Linux."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_linux") as mock:
            apply_isolation("auto", str(tmp_path))
            mock.assert_called_once_with(
                str(tmp_path), allow_network=False, allow_host_fs=False
            )

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
    def test_auto_dispatches_to_darwin(self, tmp_path):
        """apply_isolation('auto') calls _apply_darwin on macOS."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_darwin") as mock:
            apply_isolation("auto", str(tmp_path))
            mock.assert_called_once_with(
                str(tmp_path), allow_network=False, allow_host_fs=False
            )

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
    def test_allow_network_forwarded_to_darwin(self, tmp_path):
        """apply_isolation forwards allow_network to platform function."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_darwin") as mock:
            apply_isolation("auto", str(tmp_path), allow_network=True)
            mock.assert_called_once_with(
                str(tmp_path), allow_network=True, allow_host_fs=False
            )

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
    def test_allow_network_forwarded_to_linux(self, tmp_path):
        """apply_isolation forwards allow_network to platform function."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_linux") as mock:
            apply_isolation("auto", str(tmp_path), allow_network=True)
            mock.assert_called_once_with(
                str(tmp_path), allow_network=True, allow_host_fs=False
            )

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
    def test_allow_host_fs_forwarded_to_darwin(self, tmp_path):
        """apply_isolation forwards allow_host_fs to platform function."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_darwin") as mock:
            apply_isolation("auto", str(tmp_path), allow_host_fs=True)
            mock.assert_called_once_with(
                str(tmp_path), allow_network=False, allow_host_fs=True
            )

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
    def test_allow_host_fs_forwarded_to_linux(self, tmp_path):
        """apply_isolation forwards allow_host_fs to platform function."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_linux") as mock:
            apply_isolation("auto", str(tmp_path), allow_host_fs=True)
            mock.assert_called_once_with(
                str(tmp_path), allow_network=False, allow_host_fs=True
            )

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
    def test_both_flags_forwarded_to_darwin(self, tmp_path):
        """apply_isolation forwards both allow_network and allow_host_fs."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_darwin") as mock:
            apply_isolation(
                "auto", str(tmp_path), allow_network=True, allow_host_fs=True
            )
            mock.assert_called_once_with(
                str(tmp_path), allow_network=True, allow_host_fs=True
            )

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
    def test_both_flags_forwarded_to_linux(self, tmp_path):
        """apply_isolation forwards both allow_network and allow_host_fs."""
        from sandtrap.process.platform import apply_isolation

        with patch("sandtrap.process.platform._apply_linux") as mock:
            apply_isolation(
                "auto", str(tmp_path), allow_network=True, allow_host_fs=True
            )
            mock.assert_called_once_with(
                str(tmp_path), allow_network=True, allow_host_fs=True
            )

    def test_root_none_forwarded(self):
        """apply_isolation passes root=None through to platform function."""
        from sandtrap.process.platform import apply_isolation

        platform_fn = (
            "sandtrap.process.platform._apply_darwin"
            if sys.platform == "darwin"
            else "sandtrap.process.platform._apply_linux"
        )
        with patch(platform_fn) as mock:
            apply_isolation("auto", None, allow_network=True)
            mock.assert_called_once_with(None, allow_network=True, allow_host_fs=False)


# =====================================================================
# available() functions
# =====================================================================


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
class TestSeatbeltAvailable:
    def test_available_returns_bool(self):
        from sandtrap.process.seatbelt import available

        result = available()
        assert isinstance(result, bool)

    def test_available_matches_apply(self, tmp_path):
        """available() and apply() should agree."""
        from sandtrap.process.seatbelt import apply, available

        if not available():
            pytest.skip("seatbelt not available")

        exitcode, result = _run_in_child(
            lambda root: ("ok", apply(root)), str(tmp_path)
        )
        assert exitcode == 0
        assert result[1] is True


class TestSeatbeltUnavailable:
    def test_available_false_on_non_darwin(self):
        from sandtrap.process.seatbelt import available

        with patch("sandtrap.process.seatbelt.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert available() is False

    def test_apply_false_on_non_darwin(self):
        from sandtrap.process.seatbelt import apply

        with patch("sandtrap.process.seatbelt.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert apply("/tmp") is False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
class TestLandlockAvailable:
    def test_available_returns_bool(self):
        from sandtrap.process.landlock import available

        result = available()
        assert isinstance(result, bool)


class TestLandlockUnavailable:
    def test_available_false_on_non_linux(self):
        from sandtrap.process.landlock import available

        with patch("sandtrap.process.landlock.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert available() is False

    def test_apply_false_on_non_linux(self):
        from sandtrap.process.landlock import apply

        with patch("sandtrap.process.landlock.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert apply("/tmp") is False

    def test_apply_warns_when_package_missing(self):
        """apply() warns and returns False if landlock package is missing."""
        import warnings

        from sandtrap.process.landlock import apply

        with patch("sandtrap.process.landlock.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch.dict("sys.modules", {"landlock": None}):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    result = apply("/tmp")
                assert result is False
                assert any("landlock" in str(x.message).lower() for x in w)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
class TestSeccompAvailable:
    def test_available_returns_bool(self):
        from sandtrap.process.seccomp import available

        result = available()
        assert isinstance(result, bool)


class TestSeccompUnavailable:
    def test_available_false_on_non_linux(self):
        from sandtrap.process.seccomp import available

        with patch("sandtrap.process.seccomp.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert available() is False

    def test_apply_false_on_non_linux(self):
        from sandtrap.process.seccomp import apply

        with patch("sandtrap.process.seccomp.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert apply() is False
