"""Forkserver-backed workers: preload, and surviving a forked host.

``forkserver`` is what makes not-forking-from-the-host affordable. A broker is
started once, from a fresh interpreter, and forks each worker — so no worker
inherits the host's threads, and the per-worker cost is a fork rather than an
interpreter boot.

Two things make that real:

- **Preload.** Without it a worker re-imports every granted module, which is
  most of the cost (~42ms vs ~5.5ms for a 41-grant policy; a plain fork is
  ~4.8ms). The list is derived from the policy's own grants.
- **Surviving a forked host.** multiprocessing keeps the broker pid in a
  module-level singleton with no after-fork hook, so a process forked from one
  that already started a broker inherits a pid that isn't its child. The
  pre-fork server model — gunicorn, ``uvicorn --workers N`` — hits this
  whenever a supervisor forks its workers.
"""

import math
import multiprocessing
import os
import string
import sys

import pytest

from sandtrap import Policy
from sandtrap.process.sandbox import (
    ProcessSandbox,
    _apply_forkserver_preload,
    _forkserver_preload_names,
    _reset_inherited_forkserver,
)


def policy_with_grants() -> Policy:
    p = Policy(timeout=15.0)
    p.module(math)
    p.module(string)
    return p


# -- deriving the preload list ------------------------------------------------


def test_grants_are_not_preloaded_by_default():
    """Preloading runs a module's import-time code IN THE BROKER. A grant that
    starts a background thread on import leaves the broker multi-threaded, and
    a worker forked from it can inherit a lock held by that thread — the same
    permanent hang the default start method exists to prevent.

    Grants belong to the embedder, so only the embedder can vouch for them.
    """
    names = _forkserver_preload_names(policy_with_grants())
    assert names == ["sandtrap"]


def test_grants_are_preloaded_when_asked_for():
    names = _forkserver_preload_names(policy_with_grants(), include_grants=True)
    assert "math" in names
    assert "string" in names
    assert "sandtrap" in names  # the worker needs its own machinery too


def test_preload_names_use_the_module_s_real_name():
    """A grant's registration name and the module's ``__name__`` can differ;
    only the latter is importable in the broker."""
    p = Policy(timeout=5.0)
    p.module(string, name="text")
    names = _forkserver_preload_names(p, include_grants=True)
    assert "string" in names
    assert "text" not in names


def test_preload_names_skip_non_modules():
    """A live-object grant (deprecated) has no importable name — preloading it
    would take the broker down on an unresolvable import."""

    class Thing:
        pass

    p = Policy(timeout=5.0)
    with pytest.warns(DeprecationWarning):
        p.module(Thing(), name="thing")
    assert "thing" not in _forkserver_preload_names(p, include_grants=True)


def test_applying_preload_is_additive():
    """Never drop an embedder's own preload, and never let one sandbox shrink
    another's — the list is process-global and read once."""
    from multiprocessing import forkserver as fs

    before = list(getattr(fs._forkserver, "_preload_modules", ["__main__"]))
    try:
        multiprocessing.set_forkserver_preload([*before, "sandtrap_probe_sentinel"])
        _apply_forkserver_preload(["math"])
        after = set(fs._forkserver._preload_modules)
        assert "sandtrap_probe_sentinel" in after  # kept
        assert "math" in after  # added
        assert set(before) <= after  # nothing lost
    finally:
        multiprocessing.set_forkserver_preload(before)


# -- the broker actually gets used -------------------------------------------


@pytest.mark.parametrize("preload_grants", [False, True])
def test_forkserver_worker_runs_with_grants(preload_grants):
    with ProcessSandbox(
        policy_with_grants(),
        isolation="none",
        start_method="forkserver",
        preload_grants=preload_grants,
    ) as sb:
        result = sb.exec("import math\nv = math.sqrt(9)")
        assert result.error is None
        assert result.namespace["v"] == 3.0


def test_worker_is_not_a_direct_child_of_this_process():
    """Which is the point — the worker is forked by the broker, so it never
    inherits this process's threads. It also means a worker's os.getppid() is
    the broker, not the embedding process."""
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method="forkserver"
    ) as sb:
        assert sb._process.pid != os.getpid()


# -- surviving a forked host --------------------------------------------------


def _start_worker_in_forked_child(conn):
    """Runs in a process forked AFTER a broker was already started here, which
    is the pre-fork server shape: supervisor boots, then forks its workers."""
    try:
        with ProcessSandbox(
            Policy(timeout=15.0), isolation="none", start_method="forkserver"
        ) as sb:
            conn.send(("ok", sb.exec("v = 6 * 7").namespace.get("v")))
    except BaseException as exc:  # noqa: BLE001
        conn.send((type(exc).__name__, str(exc)[:200]))
    conn.close()


@pytest.mark.skipif(
    sys.platform == "win32", reason="fork-based supervisor shape is POSIX-only"
)
def test_a_process_forked_after_the_broker_started_can_still_start_workers():
    """Without the reset this fails with
    ``ChildProcessError: [Errno 10] No child processes`` — the forked child
    inherits a broker pid that is not its child."""
    # Start a broker in THIS process first; that inherited state is the hazard.
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method="forkserver"
    ) as sb:
        sb.exec("x = 1")

    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_start_worker_in_forked_child, args=(child_conn,))
    proc.start()
    child_conn.close()
    try:
        assert parent_conn.poll(60), "forked child never reported"
        status, value = parent_conn.recv()
    finally:
        proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()

    assert status == "ok", f"forked child failed: {status}: {value}"
    assert value == 42


def test_reset_does_not_touch_the_running_broker():
    """The reset must release only our inherited copies. Calling
    ForkServer._stop() instead would waitpid() a process we are not the parent
    of and unlink the other process's socket."""
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method="forkserver"
    ) as sb:
        sb.exec("x = 1")

    from multiprocessing import forkserver as fs

    _reset_inherited_forkserver()
    assert fs._forkserver._forkserver_pid is None  # our bookkeeping cleared

    # ...and the next worker simply starts a fresh broker rather than failing.
    with ProcessSandbox(
        Policy(timeout=15.0), isolation="none", start_method="forkserver"
    ) as sb:
        assert sb.exec("v = 1 + 1").namespace["v"] == 2


# -- imports must happen before the filesystem is restricted ------------------


def test_seccomp_backend_is_imported_before_landlock_restricts_the_fs():
    """Landlock must be applied first — its own setup needs syscalls seccomp
    would block — but it allows *only* the sandbox root, so reading
    ``pyseccomp.py`` out of site-packages afterwards is a PermissionError and
    the worker dies during initialisation.

    A forked worker hid this: it inherited the module in ``sys.modules``, so
    the later import was a dict hit that touched no files. A worker that starts
    fresh has to read it from disk, which made the ordering load-bearing rather
    than incidental. Asserted here because the failure only reproduces on Linux
    with Landlock available, and the ordering is what actually has to hold.
    """
    from unittest.mock import patch

    from sandtrap.process import landlock as landlock_module
    from sandtrap.process import platform as platform_module
    from sandtrap.process import seccomp as seccomp_module
    from sandtrap.sandbox import IsolationStatus

    order = []

    with (
        patch.object(
            seccomp_module,
            "preload",
            side_effect=lambda: order.append("seccomp.preload") or True,
        ),
        patch.object(
            landlock_module,
            "apply",
            side_effect=lambda root: order.append("landlock.apply") or True,
        ),
        patch.object(
            seccomp_module,
            "apply",
            side_effect=lambda **kw: order.append("seccomp.apply") or True,
        ),
    ):
        platform_module._apply_linux(
            IsolationStatus(requested=True, platform="linux"), "/tmp/box"
        )

    assert order == ["seccomp.preload", "landlock.apply", "seccomp.apply"]


def test_deferred_imports_are_warmed_before_isolation():
    """Everything sandtrap reaches lazily must be resident before Landlock
    confines the filesystem to the sandbox root — after that, a module first
    touched at exec time cannot be read at all.

    ``concurrent.futures`` is the awkward one: it resolves ``ThreadPoolExecutor``
    through a module-level ``__getattr__``, so ``net.patch.install_threading``
    triggers the import of ``concurrent.futures.thread`` on the exec path, long
    after isolation is in force.
    """
    import sys

    from sandtrap.process.worker import _warm_deferred_imports

    sys.modules.pop("concurrent.futures.thread", None)
    _warm_deferred_imports()
    assert "concurrent.futures.thread" in sys.modules
