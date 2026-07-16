# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.2.9] - 07-16-26

### Added
- **REPL-style expression echo (`echo=`).** Agents writing sandboxed code
  often expect notebook semantics — a bare top-level `x` displaying its
  value. New `echo` option on `sandbox()`/`Sandbox` (`"none"` default,
  `"last"` = Jupyter's last_expr, `"all"` = every bare top-level
  expression). Follows `sys.displayhook` conventions: repr rendering,
  `None` suppressed (so `print(x)` never double-echoes), top-level
  statements only. Echoed values land in both output channels at their
  execution position as an implicit single-arg print: repr text in
  `result.stdout` (still capped by `Policy.max_stdout`) and, with
  `snapshot_prints=True`, the raw object in `result.prints` — so
  downstream renderers treat displays and prints identically. Works
  across all isolation levels.
- **`IsolationStatus` on `ExecResult`.** Every result from a process/kernel
  sandbox carries `result.isolation` describing exactly what took effect
  (`requested`, `platform`, per-mechanism `landlock`/`seccomp`/`seatbelt`
  flags, and a `.degraded` property) so hosts can verify — not assume — the
  isolation level in force. New exports: `IsolationStatus`,
  `IsolationUnavailable`.

### Changed
- **Kernel isolation fails closed.** `isolation="kernel"` now raises
  `IsolationUnavailable` at worker startup when the platform can't apply
  the requested kernel mechanisms (missing `sandtrap[process]` packages,
  Landlock-less kernel, unsupported OS), instead of silently running user
  code with no kernel restrictions. It also fails closed when the worker
  can't *confirm* isolation (no status reported — e.g. worker version
  skew): unconfirmed is treated as failure, not a pass. Pass
  `allow_degraded=True` to proceed with reduced isolation (emits a
  `RuntimeWarning`). This is a behavior change: environments that
  previously ran kernel mode degraded-and-silent (e.g. containers without
  Landlock) will now raise unless they opt into `allow_degraded=True`.
- **Docs: kernel mode positioned honestly.** The threat model now states
  plainly that kernel mode is defense-in-depth (contains accidental/casual
  escape under cooperative code), not a boundary against actively-adversarial
  code — the inner Python layer isn't adversarial-safe and the worker→host
  IPC uses `pickle`. See `docs/roadmap.md` for the planned hardening
  (restricted deserialization / typed return contract).

### Fixed
- **`sandbox()` return type annotation.** Corrected to
  `Sandbox | ProcessSandbox` — the factory returns a `ProcessSandbox`
  (which does not subclass `Sandbox`) for `isolation="process"`/`"kernel"`,
  so type-checkers now see the actual return type.

## [0.2.8] - 2026-07-12

### Fixed
- **`RemoteFS` implements the full monkeyfs surface.** The RPC bridge
  covered core content/metadata ops but not the rest of what the
  monkeyfs patch layer can demand (`_require`): `realpath`,
  `resolve_path`, `getsize`, `samefile`, `rmdir`, `replace`, `access`,
  `lexists`, `islink`, `readlink`, `link`, `symlink`, `truncate`,
  `utime`, `chmod`, `chown`. The visible casualty: matplotlib's
  ``savefig`` calls ``os.path.realpath``, which monkeyfs routes to the
  filesystem — so ALL matplotlib chart saving raised
  "NotImplementedError: RemoteFS does not implement realpath()" under
  process/kernel isolation.

### Changed
- The import did-you-mean covers dotted imports with a wrong root
  (`import api._helpers` for /app/api/_helpers.py suggests
  `from app.api import _helpers`), and its search skips directories
  that can't appear in a dotted import path (hidden, dunder,
  non-identifier names).

## [0.2.7] - 2026-07-11

### Changed
- **Import errors now say WHERE the module actually is.** VFS imports
  resolve from `/`, and a bare `import mod` for a file living at
  `/some/dir/mod.py` failed with "Import of 'mod' is not allowed" —
  which reads as a policy ban, so agents give up on sharing code
  instead of qualifying the import. The unresolved-import error now
  distinguishes the cases: when a matching `<name>.py` exists elsewhere
  on the VFS (bounded BFS), the message reports the path and the
  working form ("Found /helpers/evdata.py — try: from helpers import
  evdata"). Truly unknown modules keep the policy message.

## [0.2.6] - 2026-07-11

### Added
- **Host-side `sys.stdout` writes are captured into `result.stdout`.**
  Registered library code that grabs the real `sys.stdout` internally —
  `df.info()` is the canonical case — used to print to the host
  process's terminal, invisible to the caller. A ContextVar-routing
  router over `sys.stdout` (the stderr router's twin, same pattern as
  the global `print` patch) now folds those writes into the executing
  context's buffer — the SAME buffer the injected `print` uses, so
  interleaving is preserved. Per-context routing means concurrent
  executions in one process don't cross-contaminate, and writes outside
  any execution fall through to the real stream untouched. Works in all
  isolation modes (the worker captures where the code runs).
- **`sandtrap.passthrough_stdio()`**: opt-out for host callbacks
  invoked from inside an execution that want the operator's real
  console (progress logging, sub-agent streaming) instead of the
  sandboxed result.

### Changed
- The contextvar-propagating threading patches (`Thread.start`,
  `ThreadPoolExecutor.submit`) now install with stdio capture as well,
  not only with network gating — capture routing follows host libraries
  into threads they spawn.

## [0.2.5] - 2026-07-11

### Added
- **`RemoteFS`: in-memory filesystems bridged over RPC in process/kernel
  mode.** Passing a non-`IsolatedFS` filesystem (e.g. `VirtualFS`) to a
  process sandbox used to fork-inherit a divergent COPY into the worker —
  sandboxed writes silently never reached the parent's instance. The
  parent now keeps the real filesystem behind an internal `__fs__` RPC
  handler and the worker sees a `RemoteFS` stub: every operation is a
  synchronous RPC, so the parent's instance stays the single source of
  truth (worker writes land in it, `chdir` moves its cwd, a worker crash
  loses nothing already written). File handles are whole-blob buffered,
  matching monkeyfs semantics — read modes fetch content once at `open`,
  writable modes buffer locally and push on `flush`/`close`; seeks,
  iteration, and partial reads are local. `IsolatedFS` keeps fork
  inheritance (parent and worker converge on the real directory, and
  kernel-level lockdown needs the host root). Wired automatically by
  `ProcessSandbox`; embedders change nothing.
- **`stdin` / `argv` cross the process boundary.** `ProcessSandbox.exec`
  / `aexec` accept the parameters the in-process sandbox gained in 0.2.3
  (they previously raised `TypeError`). `ExecMsg` carries both with
  defaults, so the wire format stays backward compatible.

### Changed
- **Worker crashes no longer disable the sandbox — `exec()` respawns.**
  A worker death (segfault, OOM, seccomp kill) returns an `ExecResult`
  carrying the error, and the next `exec()` forks a fresh worker
  transparently: a crash costs the crashing execution (and accumulated
  worker state), not the sandbox. This is what `docs/process.md` always
  said; the code raised "Worker process is not running" until re-entry.
  Clean `shutdown()` still requires re-entering the context manager.

### Fixed
- **Seatbelt (macOS kernel isolation) allows reading the Python
  installation itself.** The profile's read-only allowlist covered
  `/usr`, `/System/Library`, and `/Library` — but not interpreters
  living elsewhere (uv, pyenv, homebrew), so any *lazily* imported
  stdlib or site-packages module after lockdown died with
  `PermissionError`. The profile now grants read access to
  `sys.base_prefix` and `sys.prefix` (realpath'd), passed as profile
  parameters.
- **`filter_namespace` survives any pickling failure.** It caught only
  `PicklingError`/`TypeError`/`AttributeError`, but pickling arbitrary
  objects raises arbitrary exceptions (a closed `StringIO` raises
  `ValueError`), which escaped and killed the whole result. Any
  exception now means "dropped", never fatal. Remote file handles are
  explicitly unpicklable via `__reduce__` (the `RpcProxy` convention).

## [0.2.4] - 2026-07-09

### Added
- **Per-execution stderr capture: ``ExecResult.stderr``.** Everything
  written to ``sys.stderr`` during an execution is captured on the
  result: the synthetic sandbox ``sys.stderr`` (when ``stdin``/``argv``
  are given) and host-side writes from registered library code
  (``warnings.warn``, library diagnostics). Capture uses a router
  installed over the process's ``sys.stderr`` that delegates to the
  active execution's buffer via a ``ContextVar`` and falls through to
  the real stream otherwise — the stderr counterpart of the global
  ``print`` patch. Unlike a ``contextlib.redirect_stderr`` swap, this
  is safe under concurrent executions in one process: streams never
  cross-contaminate and the real stderr is never left pointing at a
  dead buffer. Under process/kernel isolation, stderr is captured in
  the worker and returned with the result. Embedders that redirected
  stderr around ``exec()`` themselves can drop that and read
  ``result.stderr``.

### Changed
- **Synthetic ``sys.stderr`` no longer merges into ``stdout``.** With
  ``stdin``/``argv`` given, ``sys.stderr.write(...)`` from sandboxed
  code now lands in ``result.stderr`` instead of ``result.stdout``.

## [0.2.3] - 2026-07-06

### Added
- **Synthetic safe `sys` + stdin-backed `input()`.** ``exec`` / ``aexec``
  gain ``stdin`` (a ``str`` or text stream) and ``argv`` (a list); passing
  either exposes a minimal, safe ``sys`` to the sandboxed code —
  ``sys.stdin``, ``sys.stdout`` / ``sys.stderr`` (routed to the captured
  output), and ``sys.argv`` — plus an ``input()`` that reads from it. So
  real idioms work (``for line in sys.stdin``, ``sys.stdout.write(...)``,
  ``input()``, ``sys.argv``) while interpreter internals stay unreachable:
  the object exposes only those four attributes and never references the
  real ``sys`` (``sys.modules`` / ``settrace`` / ``exit`` / ``path`` all
  raise ``AttributeError``). Behavior is unchanged when neither param is
  given — ``import sys`` stays blocked and ``input()`` stays unavailable.

## [0.2.2] - 2026-07-06

### Fixed
- **Recursive registrations now police attribute traversal into
  submodules.** Submodule objects reached through a ``recursive=True``
  parent carried no registration in ``is_attr_allowed``, so
  ``numpy.random.seed(0)`` sailed past an exclude that
  ``from numpy.random import seed`` already enforced. Submodules now
  inherit the parent registration (filters included), and submodule
  *imports* honour the parent's excludes too — dotted patterns against
  the full path, bare patterns against the terminal segment (the
  default ``"_*"`` now blocks ``import numpy._core``).

  Behavior note: a narrow ``include=`` on a recursive registration now
  constrains submodule attribute access as well (previously it only
  constrained from-imports).

### Added
- **Dotted (owner-qualified) patterns.** ``include``/``exclude``
  patterns containing a dot match qualified names:
  ``"DataFrame.eval"`` (class-qualified, checked through the MRO) and
  ``"numpy.random.seed"`` / ``"pandas.core*"``
  (module-path-qualified). Previously such patterns never matched
  anything — predicates only ever saw bare member names. Bare patterns
  keep their existing meaning; callables receive bare names only.

## [0.2.1] - 2026-04-29

### Added
- **Worker → parent RPC channel.** ``ProcessSandbox`` now accepts an
  ``rpc_handlers: dict[str, Callable[[str, tuple, dict], Any]]``
  argument (also exposed via ``sandbox(...)``).  Inject
  ``RpcProxyMarker(target=...)`` into the namespace and the worker
  substitutes it with an ``RpcProxy`` (or a wrapper class instance,
  via ``marker.wrapper="module:Class"``) whose method calls are
  forwarded over the existing parent-worker connection to the
  registered handler.  The parent's ``exec`` dispatch loop runs each
  ``RpcCallMsg`` to completion and replies with ``RpcReturnMsg``
  before returning to waiting on ``ResultMsg``.

  This is the mechanism agex (≥ 0.13) uses to give the agent a
  working ``cache`` under process / kernel isolation: the worker
  sees a proxy in its namespace, the parent's handler dispatches to
  the live ``Cache(state)`` in the parent process, and writes
  propagate naturally.  The protocol generalises — any host-side
  resource that follows the ``handler(method, args, kwargs) →
  value`` shape works the same way.

  New exports: ``RpcProxyMarker`` from the package root.  Internal
  message types ``RpcCallMsg`` / ``RpcReturnMsg`` live in
  ``sandtrap.process.protocol``.

  Forward-compatibility: the dispatch loop warns on unknown message
  types instead of failing, so future protocol additions (e.g.
  streamed prints) won't break existing parents.

### Fixed
- ``RpcProxy.__reduce__`` raises ``PicklingError`` so
  ``filter_namespace`` drops it from result namespaces.  Without
  this the worker could try to pickle a Connection-bearing proxy on
  the way back to the parent, hitting a syscall blocked by Seatbelt
  under kernel isolation.
- The exec dispatch loop now extends the wall-clock deadline by
  exactly the host-side handler's duration on each ``RpcCallMsg``,
  rather than resetting it to ``timeout + grace`` afresh.  The
  reset variant let a worker dodge the sandbox timeout by spamming
  cheap RPC calls (each one granting a new full budget); the
  duration-only extension credits back only the parent-side time
  consumed, so the worker's own execution still has to fit within
  the original budget.

## [0.2.0] - 2026-04-29

### Added
- **`__sandtrap_activate__` container hook.** ``Sandbox._auto_activate``
  now invokes
  ``v.__sandtrap_activate__(activate_value, gates, sandbox, namespace)``
  on any host-side namespace value that exposes the method, giving
  containers (e.g. agex's ``Cache``) a chance to walk and activate
  sandbox-defined values they hold one level below the namespace top.
  The ``namespace`` argument lets nested wrappers resolve late-bound
  globals via ``activate_value(..., namespace=namespace)``.  Hook
  exceptions are swallowed so a misbehaving container can't break
  ``exec``.  The hook is **not** invoked on ``StFunction`` /
  ``StClass`` / ``StInstance`` / ``ModuleRef`` — sandboxed wrappers
  are untrusted, and exposing the live ``gates`` dict to one would
  permit a sandbox escape.

### Removed
- **`find_refs` and the `refs` module.** The static reference analyzer
  was used by agex for selective state hydration and mutation
  detection in the old cross-emission persistence model; agex no
  longer hydrates state into the namespace, so the analyzer has no
  consumers. Removed with it: ``StFunction.global_refs`` property,
  the ``_global_ref_names`` pickle slot, and the ``tests/test_refs.py``
  + selective-restore stress tests that exercised them.
  ``StFunction._frozen_globals`` (which holds the actual sandbox
  -defined ``StFunction``/``StClass`` values for re-activation) is
  unchanged.

## [0.1.15] - 2026-04-28

### Fixed
- **`from X import Y` submodule access now respects `recursive=`.**
  `resolve_module_member` resolved submodules via direct attribute
  lookup with no policy gate, so `from os import path` slipped past
  a non-recursive `os` registration whenever the parent already had
  the submodule bound (eager case: `os.path`, `email.mime`, ...).
  Submodule access — eager or lazy — now goes through
  `is_import_allowed`, the same as `import X.Y` would. Also adds
  the lazy `importlib.import_module` fallback so `from PIL import
  ImageDraw` works against `recursive=True` parents that don't
  eager-import their submodules.

## [0.1.14] - 2026-04-09

### Fixed
- **Recursive module registration network access for class instances**: Instances of classes from recursively registered modules were denied network access because `_find_registration_for` didn't check `type(obj).__module__` against recursive module registrations. Method calls on those instances now correctly inherit `network_access` and `host_fs_access` from the module's registration.

## [0.1.13] - 2026-04-01

### Fixed
- **`ThreadPoolExecutor.map` concurrent context crash**: The context propagation patch shared a single `Context` object across all map workers, causing `RuntimeError: cannot enter context` when workers ran concurrently. Removed the redundant `map` patch entirely — CPython's `Executor.map` delegates to `self.submit` for each item, so the `submit` patch handles context propagation correctly.

## [0.1.12] - 2026-04-01

### Fixed
- **`from X import Y` privilege escalation**: `from module import func` bypassed `network_access` and `host_fs_access` wrapping because `__st_importfrom__` returned raw callables without privilege checks. Import-time resolution now applies the same wrapping as attribute access, including per-member `configure` overrides.

## [0.1.11] - 2026-04-01

### Fixed
- **ContextVar propagation to worker threads**: Patched `threading.Thread.start`, `ThreadPoolExecutor.submit`, and `ThreadPoolExecutor.map` to snapshot and propagate `contextvars` to worker threads. This ensures `network_allowed` (and other ContextVars like `current_fs`) are inherited correctly when registered functions dispatch work to thread pools.
- **Patch installation resilience**: Each socket and threading patch now guards against partial installation, preventing infinite recursion if `install()` is retried after a mid-install failure.

## [0.1.10] - 2026-03-13

### Fixed
- **`print`/`help`/`open` in VFS helper modules**: Modules loaded via the virtual filesystem were missing `print`, `help`, and `open` in their builtins, causing `NameError` when agents used these in helper modules. All three are now injected into VFS module builtins.
- **Frozen VFS module builtins**: VFS module builtins are now wrapped in `_FrozenBuiltins` to prevent sandboxed code from mutating them, matching the main sandbox behavior.

## [0.1.9] - 2026-03-13

### Added
- **Print redirection for registered functions**: `print()` calls from registered functions, their callees, and any library code during sandbox execution now route to the sandbox stdout buffer instead of the host's real stdout. Uses a `ContextVar` + context manager, matching the existing pattern for network denial.

## [0.1.8] - 2026-03-12

### Added
- **`from main import X` support**: Sandboxed code can use `from main import X` or `from __main__ import X` to reference names defined earlier in the sandbox namespace, matching a common LLM code pattern.
- **`dir()` override in `aexec`**: `dir()` with no arguments now includes sandbox namespace globals instead of returning interpreter internals.

### Fixed
- **`__import__` in VFS module builtins**: Modules loaded via the virtual filesystem now have access to `__import__`, allowing nested imports to work correctly.
- **`print` and `help` in module builtins**: Injected into builtins so imported modules can use them without explicit registration.
- **Lazy submodule resolution**: Recursive module registrations now fall back to `importlib.import_module()` for submodules not yet loaded as parent attributes.
- **`dir()` sentinel**: Use a proper `object()` sentinel instead of the `_builtins` module reference.
- **Top-level imports**: Moved `sys` and `importlib` imports from inline to module level.

## [0.1.7] - 2026-03-02

### Added
- **Raw mode context capture**: Functions, lambdas, and class methods defined in raw mode automatically capture sandbox ContextVars (`current_fs`, `network_allowed`) at definition time and restore them on every call. This ensures NiceGUI callbacks and other deferred invocations retain filesystem and network isolation after `sb.exec()` returns.

### Fixed
- **Checkpoint timer for raw mode callbacks**: Each outermost callback invocation resets the checkpoint timer and tick counter, giving it a fresh budget instead of accumulating from `sb.exec()` start time.
- **Timeout bypass via nested calls**: Nested function calls within a callback no longer reset the execution budget. Only the outermost callback entry gets a fresh budget.
- **Timeout bypass via function calls in loops**: During `sb.exec()`, calling wrapped functions in a loop no longer resets the checkpoint timer on each iteration.
- **Decorator ordering**: Multiple decorators on context-captured functions are now applied in the correct bottom-up order, matching Python semantics.
- **Python 3.14 compatibility**: Replaced deprecated `asyncio.iscoroutinefunction` with `inspect.iscoroutinefunction`.

## [0.1.6] - 2026-02-28

### Fixed
- **ProcessSandbox worker respawn**: Dead workers now raise RuntimeError instead of silently re-forking, preventing deadlocks when threads are running
- **Bare assert in sandbox.py**: Replaced with proper RuntimeError guard

### Changed
- **Bare except: rewrite**: Documented rationale in security.md and rewriter docstring
- **monkeyfs dependency**: Pinned to <0.2.0
