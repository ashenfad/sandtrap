# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.3.2 - 2026-08-21

### Changed

- **The `process` extra is now `kernel`:** `pip install sandtrap[kernel]`.

  It was misnamed from the start. Its contents are `landlock` and `pyseccomp`,
  both behind `sys_platform == 'linux'` — so it is the *Linux kernel-mode*
  extra. It has nothing to do with `isolation="process"`, which needs no extras
  on any platform, and nothing to do with kernel mode on macOS, where Seatbelt
  ships with the OS. The old name told readers to install it for crash
  containment they already had, and told mac users to install a package set
  that resolves to nothing for them.

  Renamed rather than aliased because nothing depends on it yet. If you do have
  it pinned, pip warns (`does not provide the extra 'process'`) and installs
  without the bindings; `isolation="kernel"` on Linux then fails closed with
  `IsolationUnavailable`, as it always has. Two loud signals, no silent loss of
  protection.

### Fixed

- **`sandbox()` can now express `start_method` and `preload_grants`.** Both are
  public `ProcessSandbox` arguments, and neither was reachable through the
  factory — which matters because `sandbox` is the only sandbox constructor in
  `sandtrap.__all__`.

  For `start_method` that made a documented remedy unusable. 0.3.0 refuses a
  policy that can't be serialized to a non-forked worker, and says so:

  ```
  StPolicyNotPortable: This policy cannot be sent to a
  start_method='forkserver' worker ...
  Use start_method="fork" to keep inheriting memory ...
  ```

  Following that advice from the public API was impossible; it required
  importing `sandtrap.process.sandbox.ProcessSandbox`, which is not exported.
  So 0.3.0 shipped a breaking change whose escape hatch only worked if you
  reached past the front door.

  `preload_grants` was unreachable the same way, making the 16×-faster,
  4×-smaller worker configuration documented in 0.3.1 inapplicable to anyone
  using the factory.

  Both are ignored under `isolation="none"`, matching how `rpc_handlers`,
  `allow_degraded`, and `close_fds` already behave there — so one config can
  drive every rung.

- The factory's `isolation` docs still described `"process"` and `"kernel"` as
  forking a worker. They haven't since 0.3.0.

### Documentation

- **`Policy.check_picklable()` is documented.** It shipped as a headline 0.3.0
  addition and appeared nowhere user-facing — only in an internal design note.
  `policy.md` now covers it, with what crosses by name and what doesn't, and
  the `PolicyProblem` fields.
- **The policy-portability requirement is in the README.** Reaching for
  `isolation="process"` from the README alone could produce a
  `StPolicyNotPortable` the README never hinted at.
- `sandbox.md`'s parameter list — which `process.md` calls the canonical
  reference — stopped before every worker-only argument, so `rpc_handlers`,
  `allow_degraded`, and `close_fds` were undocumented there too. All five are
  now listed, with the ignored-under-`isolation="none"` rule stated once.

## 0.3.1 - 2026-08-21

### Added

- **`preload_grants` warns when it can't take effect.** multiprocessing reads
  the forkserver preload list once, when the broker starts, so only the first
  sandbox to start a worker in a process can set it. Asking afterwards now
  emits a `RuntimeWarning` naming the modules that won't be inherited.

  The behaviour is unchanged and was always correct — later sandboxes work,
  their grants are simply imported per worker. But it was *silent*, and it
  presents exactly as the flag being broken: `preload_grants=True` is accepted
  and worker start stays slow. It fooled us on our own benchmark, where the
  measured difference was 234ms vs 238ms until each case was run in a fresh
  process, whereupon it was 14ms vs 233ms.

  In practice this makes `preload_grants` a process-wide setting wearing
  per-sandbox clothes. A host building one sandbox per session should set it
  uniformly, or set it on the first.

### Fixed

- **`ProcessSandbox`'s docstring said granted modules are preloaded into the
  broker by default. They are not** — that describes `preload_grants=True`,
  which is opt-in. The quoted cost ("~0.7ms per worker over a plain fork") was
  the opt-in figure, so a reader sizing a system from the class docs
  underestimated worker start by more than two orders of magnitude on a
  heavyweight policy. `docs/process.md` and the CHANGELOG had it right; only
  the docstring was stale.
- `policy:` in the same docstring still described being "inherited by the child
  process via fork", which stopped being the default in 0.3.0.
- **The live-object deprecation named a version that had already shipped.** It
  announced removal "in 0.3" — the release that introduced the deprecation —
  so anyone planning against it read a date that was already past. Retargeted
  to **0.4.0**, the next version permitted to break, in the warning text,
  `docs/policy.md`, and the 0.3.0 changelog entry. Live-object grants are
  unchanged and still work; only the removal date they promise has moved.

### Documentation

- `preload_grants` now has a parameter entry at all — it was a public keyword
  argument documented nowhere user-facing, with its rationale readable only in
  a private helper.
- Recorded what granting a heavyweight stack actually costs, since the existing
  table measures a stdlib policy and the gap is much wider than it suggests:
  with pandas/numpy/plotly/matplotlib granted, a worker is **~235ms and ~113MB**
  by default against **~14ms and ~29MB** with `preload_grants=True`. Preloaded
  modules are shared copy-on-write from the broker, so the stack is paid for
  once rather than per worker — which is what makes the memory difference, and
  what a pool of resident workers should be sized against.

## 0.3.0 - 2026-08-20

### Changed

- **BREAKING: process/kernel workers are no longer forked from the embedding
  process.** The default `start_method` is now `forkserver` on POSIX (`spawn`
  where forkserver doesn't exist). A broker is started once from a fresh
  interpreter and forks each worker; because the broker never grows threads, no
  worker can inherit a lock the host holds.

  This closes a failure that had no good diagnosis: forking a multi-threaded
  host — a uvicorn server, browser automation, anything with a thread pool —
  can hand the child a lock held by a thread that doesn't exist in it. The
  child then *hangs* rather than crashing, until the caller's own timeout fires
  and reports `"Worker process became unresponsive"`, which names none of it.
  Confirmed at kernel level in production: every thread of a hung worker parked
  on `futex_wait_queue` with no voluntary context switches.

  The broker preloads sandtrap itself, putting a worker at about **18ms**
  against 4.8ms for a plain fork (and 42ms with nothing preloaded, 77ms for
  `spawn`). Passing `preload_grants=True` also preloads your granted modules
  and brings that to ~5.5ms — off by default, because preloading runs those
  modules' import-time code *in the broker*, and a grant that starts a thread
  on import would leave the broker multi-threaded and reintroduce the very
  hang this change removes.

  **What this requires of your policy.** A worker that doesn't inherit memory
  needs the policy serialized to it, so registrations must be reachable by
  name. Checked at construction — `ProcessSandbox` raises `StPolicyNotPortable`
  listing *every* problem, rather than surfacing pickle's first failure at
  worker start. Most policies are unaffected: module grants, module-level
  functions, and classes all cross by name. What doesn't: lambdas and closures,
  bound methods and callable instances, dynamically created modules, callable
  `include`/`exclude` predicates, and classes defined inside a function.

  `start_method="fork"` remains available as the escape hatch — explicitly,
  never as a silent fallback, since falling back quietly would put you back on
  the hanging path without saying so.

- **Live-object grants are deprecated** (removal in 0.4.0 — this entry said
  "0.3" on release, which was this same version; corrected in 0.3.1).
  `policy.module(obj)`
  and `policy.fn(obj.method)` pinned a live object inside the policy. Register
  the **class** and bind the instance in the exec namespace instead — same
  member filters, same per-member privileges, and the policy stays portable.

  Under `isolation="process"`/`"kernel"` the old form never did what it looked
  like: fork handed the worker a copy-on-write *snapshot*, so mutations
  sandboxed code made never reached the host object. This closes a silent bug
  rather than removing a working feature.

### Added

- **`Policy.check_picklable()`** — every reason a policy can't reach a
  non-forked worker, in one pass, with the registration named and a remedy
  attached. Reasons about *importability*, not just serializability: a module
  built at runtime pickles happily and fails to load on the other side.
- **`sandtrap.rpc_surface(obj, policy=None)`** and `RpcProxyMarker(methods=,
  attributes=)` — declare a bridged object's surface so the worker's proxy can
  answer for it. Without a declared surface a proxy returns a caller for
  *every* name, so reading `obj.token` silently yielded a function (and `None`
  by the time it crossed back), and `obj.token = x` was silently lost. Both are
  now clear `AttributeError`s. Passing the policy narrows the surface to what
  it permits, which is the only place a policy's member filters reach a bridged
  object.
- **`IsolationStatus` reports `allow_network`, `allow_host_fs`, and `root`** —
  what the worker was actually built with, not only which mechanisms engaged.
- **`ProcessSandbox(start_method=...)`** — `"forkserver"`, `"spawn"`, or
  `"fork"`, defaulting to the safest available.

### Fixed

- **A worker that died during init was diagnosed as fork-hostile regardless of
  how it was created.** A spawned worker inherits nothing, so advice to
  "construct the sandbox earlier" or set `ARROW_DEFAULT_MEMORY_POOL` sent
  people to fix a process they weren't forking. `StForkUnsafe` is now raised
  only for genuinely forked workers.
- **Assignment to an RPC proxy no longer silently vanishes.** `obj.attr = x`
  set an attribute on the worker-side stand-in and left the host object
  untouched, with nothing to indicate the write went nowhere.

## [0.2.14] - 2026-08-04

### Added
- **`StForkUnsafe`: a named error for fork-hostile hosts.** A process worker
  that dies before signalling ready used to raise a bare
  `RuntimeError("Worker process died during initialisation")`, and the
  automatic respawn re-forked the same hostile host — producing a permanent,
  unexplained loop. That signature now raises `StForkUnsafe`, reporting the
  worker's exit signal and the host's live thread count, and listing the
  fixes (construct the sandbox earlier, set `ARROW_DEFAULT_MEMORY_POOL=system`
  for pyarrow/pandas hosts, or drop to `isolation="none"`). The classification
  is narrow: only a worker that dies before `ReadyMsg` *from a native crash
  signal* (`SIGSEGV`, `SIGBUS`, `SIGABRT`, …) is diagnosed as fork hostility.
  A ready-timeout, a clean `WorkerErrorMsg`, a nonzero exit, and an external
  `SIGKILL` each get their own message, since none of them indicates a
  fork-broken host. The error subclasses both `StError` and `RuntimeError`, so
  existing handlers keep working. Recovering automatically is tracked in
  [#33](https://github.com/ashenfad/sandtrap/issues/33).

### Fixed
- **Worker setup failures now report their own traceback.** `_worker_entry`
  performs fallible work — inherited-descriptor neutralization (`close_fds=True`)
  and importing the worker module — before `worker_main` installs its exception
  reporting. A failure there reached the parent as a bare EOF, so an ordinary
  setup error (`EMFILE`, a broken install) was indistinguishable from a worker
  that died in C-library setup. Those failures are now caught and sent as
  `WorkerErrorMsg`, surfacing the actual traceback instead of a misleading
  diagnosis.

### Changed
- **Requires monkeyfs >= 0.1.6.** The floor was previously unbounded below, so
  an old monkeyfs could satisfy the dependency while leaving filesystem
  interception bypassable. 0.1.6 closes three gaps that matter directly to
  sandboxed code: `bytes` and `os.PathLike` paths skipped interception
  entirely and reached the host (`open(b"/etc/passwd").read()` was enough),
  `dir_fd` arguments resolved against the host filesystem, and the safe-path
  passthrough handed out real host directory descriptors. It also fixes
  `os.mkdir()` raising `TypeError` against any filesystem implementing the
  documented `FileSystem` protocol — which includes sandtrap's own `RemoteFS`,
  so `os.mkdir()` from sandboxed code under `isolation="process"` was broken
  for every non-`IsolatedFS` filesystem.

## [v0.2.13] - 07-30-26

### Added
- **Opt-in ambient file-descriptor cleanup for process workers.**
  Both process and kernel workers used to inherit every descriptor open at
  fork time, including listening/accepted sockets, database connections, and
  other sandboxes' control channels. Those duplicates could suppress EOF,
  keep host resources alive, and become reachable if a policy later granted
  low-level descriptor access. Pass `close_fds=True` to neutralize ambient
  descriptors while preserving standard streams and private IPC.
  The backward-compatible default remains `False`: policy registrations may
  intentionally use a fork-inherited live resource. Such registrations must
  bridge the resource explicitly through RPC before enabling cleanup.

### Fixed
- **Idle workers exit when their parent process disappears.** Workers
  inherited the parent endpoints for their own and earlier sandboxes'
  `multiprocessing.Pipe` connections. Those duplicates prevented an idle
  worker from observing control-channel EOF after an abrupt host exit,
  especially while a later worker remained busy. Each child now closes every
  active Sandtrap parent endpoint inherited at fork.

## [v0.2.12] - 07-20-26

### Added
- **`Policy.module_root` — a configurable base for VFS imports.**
  Filesystem-module imports hardcoded `/` as their resolution root, so
  a host presenting the workspace under a prefix had `import mod` miss
  the `<root>/mod.py` that sandboxed `open()` sees — and local
  sandboxes diverged from VM guests, where imports already resolve
  from the guest workspace dir. `module_root` (default `/`) sets that
  base; imports resolve root-relative, so `<root>/helpers/foo.py` is
  `helpers.foo` and the root's own name stays out of the import
  namespace. Fully backward compatible, and it rides the existing
  policy pickle across process-isolation workers. Import errors name
  the configured root, derive their did-you-mean relative to it, and
  call out a hit found outside the root as unreachable rather than
  suggesting a dotted path that would also fail.

### Documentation
- **Fork safety in `docs/process.md`.** Workers fork from the embedding
  process and respawn re-forks the *current* process, which a
  long-lived host (threads plus fork-hostile C state) can make lethal.
  Documents the invariant, the failure signature, and the pyarrow
  mimalloc trap along with the embedder-side fix.

## [v0.2.11] - 07-17-26

### Added
- **Per-exec `echo` override.** `Sandbox.exec()`/`aexec()` (and their
  `ProcessSandbox` counterparts) accept `echo=` to override the
  construction-time mode for a single call (`None` keeps the default).
  One sandbox can now serve a notebook-style surface (`echo="last"`)
  and a script-semantics surface (`echo="none"`) without paying for
  two workers under process isolation. Invalid values raise host-side,
  before any code runs.

## [v0.2.10] - 07-17-26

### Fixed
- **Tracebacks now survive process isolation.** Traceback objects can't
  be pickled, so a runtime error crossing the worker→parent pipe
  arrived as a bare message — no frames, no line numbers, nothing for
  an agent's repair loop to aim at. The worker now renders the full
  traceback where the frames still exist and attaches the text to the
  exception as `_st_traceback_text` (riding `BaseException.__reduce__`'s
  `__dict__` preservation). Hosts that want frames read the attribute;
  everything else is unchanged. Exceptions that refuse the attribute
  (`__slots__`, frozen) degrade to today's message-only behavior.
- **Unpicklable exceptions degrade to a stand-in, not worker noise.**
  An exception carrying unpicklable baggage killed the `ResultMsg`
  send, burying the agent's actual error under a "Worker error"
  pickling traceback. The worker now pickle-checks the error and ships
  a `RuntimeError` stand-in carrying the original class name, message,
  and rendered traceback text. (Surfaced by PR #31 review.)

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
