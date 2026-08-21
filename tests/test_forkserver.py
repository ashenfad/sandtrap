"""Forkserver-backed workers: preload, and surviving a forked host.

``forkserver`` is what makes not-forking-from-the-host affordable. A broker is
started once, from a fresh interpreter, and forks each worker — so no worker
inherits the host's threads, and the per-worker cost is a fork rather than an
interpreter boot.

Two things make that real:

- **Preload.** The broker preloads ``sandtrap`` itself, taking a worker from
  ~42ms to ~18ms (a plain fork is ~4.8ms). Grants are NOT preloaded by
  default — ``preload_grants=True`` adds them and reaches ~5.5ms, at the cost
  of running their import-time code in the broker.
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
import warnings

import pytest

from sandtrap import Policy
from sandtrap.process import sandbox as sandbox_mod
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
        # Whether this grows the set after a broker is up depends on what ran
        # earlier in the session, so the already-running warning is expected
        # here in a full run and absent in an isolated one. It has its own
        # tests above; this one is about the union.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            _apply_forkserver_preload(["math"])
        after = set(fs._forkserver._preload_modules)
        assert "sandtrap_probe_sentinel" in after  # kept
        assert "math" in after  # added
        assert set(before) <= after  # nothing lost
    finally:
        multiprocessing.set_forkserver_preload(before)


# -- growing the list after the broker is up ----------------------------------
#
# The list is read ONCE, at broker start. A later sandbox asking for more gets
# it on the module-level list but not into the running broker, so its workers
# import those modules themselves. That is correct and documented — but silent,
# and it presents as `preload_grants=True` doing nothing at all (the flag is
# accepted; worker start just stays slow). These pin the warning that says so.


BROKER_PID = 12345


def _with_recorded_broker(loaded, fn, *, owner=None, requested=None):
    """Run ``fn`` against a broker that LOADED ``loaded``, then restore state.

    Fakes the record rather than starting a real broker: whether one is
    already up otherwise depends on what ran earlier in the session.

    ``owner`` defaults to this process. Pass a different pid to model a record
    inherited through fork — the parent's broker, which this process is about
    to replace. ``requested`` seeds the module-level *requested* list, which is
    deliberately allowed to differ from ``loaded``: that divergence is the
    whole point of tracking them separately.
    """
    from multiprocessing import forkserver as fs

    before_names = list(getattr(fs._forkserver, "_preload_modules", ["__main__"]))
    before_pid = getattr(fs._forkserver, "_forkserver_pid", None)
    before_record = sandbox_mod._BROKER_PRELOAD
    try:
        multiprocessing.set_forkserver_preload(
            list(loaded) if requested is None else list(requested)
        )
        fs._forkserver._forkserver_pid = BROKER_PID
        sandbox_mod._BROKER_PRELOAD = (
            os.getpid() if owner is None else owner,
            BROKER_PID,
            frozenset(loaded),
        )
        return fn()
    finally:
        sandbox_mod._BROKER_PRELOAD = before_record
        fs._forkserver._forkserver_pid = before_pid
        multiprocessing.set_forkserver_preload(before_names)


def test_asking_a_running_broker_for_a_module_it_lacks_warns():
    with pytest.warns(RuntimeWarning, match="already running") as caught:
        _with_recorded_broker(
            ["__main__", "sandtrap"],
            lambda: _apply_forkserver_preload(["sandtrap", "pandas_stand_in"]),
        )
    # names the module that won't be inherited, so the reader can act on it
    assert "pandas_stand_in" in str(caught[0].message)
    assert "sandtrap" not in str(caught[0].message)  # that one IS loaded


def test_every_unserved_request_warns_not_only_the_first():
    """Regression: the warning used to trigger on the REQUESTED list growing.

    The first unserved request appends to that list (correctly — a broker
    started later should honour it), so a second sandbox asking for the same
    module found it already there and said nothing, while being just as
    unserved. For a host building one sandbox per session that meant exactly
    one warning and then silence, which is the pattern most likely to be
    missed. What matters is what the broker LOADED, and that never changes
    while it runs.
    """
    seen = []

    def two_sandboxes_in_a_row():
        for _ in range(2):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _apply_forkserver_preload(["sandtrap", "pandas_stand_in"])
            seen.append([str(w.message) for w in caught])

    _with_recorded_broker(["__main__", "sandtrap"], two_sandboxes_in_a_row)

    assert len(seen[0]) == 1, "first request should warn"
    assert len(seen[1]) == 1, "second request is equally unserved and must warn"
    assert all("pandas_stand_in" in m for group in seen for m in group)


def test_no_warning_when_the_broker_already_carries_the_preload():
    """The common case for a multi-workspace host: every sandbox asks for the
    same preload, the first one's request took effect, and the rest inherit
    it. Warning on those would be noise, not signal."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning fails the test
        _with_recorded_broker(
            ["__main__", "sandtrap", "math"],
            lambda: _apply_forkserver_preload(["sandtrap", "math"]),
        )


def test_no_warning_before_the_broker_starts():
    """The path that actually works — set the list, then start the broker."""
    from multiprocessing import forkserver as fs

    before_record = sandbox_mod._BROKER_PRELOAD
    before_pid = getattr(fs._forkserver, "_forkserver_pid", None)
    try:
        sandbox_mod._BROKER_PRELOAD = None  # no broker of ours
        fs._forkserver._forkserver_pid = None
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _apply_forkserver_preload(["sandtrap", "math"])
    finally:
        fs._forkserver._forkserver_pid = before_pid
        sandbox_mod._BROKER_PRELOAD = before_record


def test_no_warning_for_a_broker_inherited_through_fork():
    """Regression: pre-fork servers (gunicorn, ``uvicorn --workers N``) warned
    in every worker.

    A forked child inherits both the broker pid and the requested list, but
    the pid names a process that is not its child — so its first worker start
    raises ``ChildProcessError``, resets, and gets a broker of its own that
    DOES honour the current list. Warning there is a false positive, and it
    would fire once per worker on every deployment of that shape. The record's
    owner pid is what distinguishes the two.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _with_recorded_broker(
            ["__main__", "sandtrap"],
            lambda: _apply_forkserver_preload(["sandtrap", "pandas_stand_in"]),
            owner=os.getpid() + 1,  # recorded by the process we were forked from
        )


def test_a_replaced_broker_invalidates_the_record():
    """If the live pid isn't the one we recorded, we know nothing about what
    the current broker holds — so say nothing rather than guess."""
    from multiprocessing import forkserver as fs

    def swap_the_broker_then_ask():
        fs._forkserver._forkserver_pid = BROKER_PID + 1
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _apply_forkserver_preload(["sandtrap", "pandas_stand_in"])

    _with_recorded_broker(["__main__", "sandtrap"], swap_the_broker_then_ask)


# -- the broker actually gets used -------------------------------------------


@pytest.mark.parametrize("preload_grants", [False, True])
def test_forkserver_worker_runs_with_grants(preload_grants):
    """Both settings produce a WORKING worker — which is the whole assertion
    here, and is true regardless of which one wins the broker.

    Expect the ``True`` case to emit the already-running warning: the ``False``
    case ran first and started the broker, so this one's grants can't be
    preloaded. That is the documented behaviour, not a failure — the worker
    imports them itself. Whether a preload actually took effect is timing
    within one process, so it belongs to the unit tests above (which fake the
    broker) rather than here.
    """
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
