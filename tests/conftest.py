"""Suite-wide options.

``--start-method`` runs the whole suite against workers created a different
way. It exists for the forkserver work (``docs/forkserver-design.md``): the
process suite is already the best description of what a worker must do, so
re-running it under ``spawn``/``forkserver`` is a differential against the
forked worker rather than a second suite to write and keep in sync.

    pytest tests                          # fork, the shipped default
    pytest tests --start-method=spawn     # same suite, nothing inherited

Tests that are *about* fork inheritance mark themselves ``@pytest.mark.fork_only``
and skip elsewhere.
"""

import pytest

START_METHODS = ("fork", "spawn", "forkserver")


def pytest_addoption(parser):
    parser.addoption(
        "--start-method",
        default="default",
        choices=("default", *START_METHODS),
        help="How ProcessSandbox workers are created for this run. "
        "'default' leaves the shipped choice alone.",
    )


def _effective(config) -> str:
    """The method this run actually uses, resolving 'default'.

    Needed because ``fork_only`` has to skip whenever the run isn't forking,
    and since the shipped default became forkserver, a plain ``pytest`` run
    isn't.
    """
    from sandtrap.process.sandbox import default_start_method

    chosen = config.getoption("--start-method")
    return default_start_method() if chosen == "default" else chosen


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "fork_only: depends on the worker inheriting the parent's memory, so "
        "it is meaningless under spawn/forkserver",
    )
    config.addinivalue_line(
        "markers",
        "no_forkserver: builds a fake host by forking, which multiprocessing's "
        "forkserver state does not survive (see docs/forkserver-design.md)",
    )


@pytest.fixture(scope="session")
def start_method(pytestconfig) -> str:
    return pytestconfig.getoption("--start-method")


@pytest.fixture(autouse=True)
def _apply_start_method(request, monkeypatch):
    """Make every ProcessSandbox in this run use the selected start method.

    Patched rather than threaded through ~128 call sites: the point is to run
    the *existing* tests unchanged, so that a difference in result is a
    difference in the worker and not in the test. ``setdefault`` leaves tests
    that pass ``start_method`` explicitly alone.
    """
    method = _effective(request.config)

    if method != "fork" and request.node.get_closest_marker("fork_only"):
        pytest.skip(f"fork-only test, running under {method}")

    if method == "forkserver" and request.node.get_closest_marker("no_forkserver"):
        pytest.skip("test forks a fake host; forkserver state doesn't survive that")

    if request.config.getoption("--start-method") == "default":
        return  # exercise what ships, unpatched

    from sandtrap.process.sandbox import ProcessSandbox

    original = ProcessSandbox.__init__

    def _init(self, *args, **kwargs):
        kwargs.setdefault("start_method", method)
        original(self, *args, **kwargs)

    monkeypatch.setattr(ProcessSandbox, "__init__", _init)
