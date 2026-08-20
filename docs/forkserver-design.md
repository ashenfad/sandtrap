# Forkserver-backed workers (design)

Working notes for the work tracked in [#33](https://github.com/ashenfad/sandtrap/issues/33)
and [#38](https://github.com/ashenfad/sandtrap/issues/38) — the plan, the evidence
behind it, and the decisions made along the way, so none of it has to be
re-derived.

**Status: shipped as the default.** Workers are created by `forkserver` on
POSIX (`spawn` elsewhere); `fork` is the explicit escape hatch. The process
suite passes under all four settings, and the preload keeps the cost at ~5.5ms
per worker against fork's ~4.8ms.

Step 11 was expected to gate this and largely dissolved instead: the object half
turned out to be expressible with the *existing* `policy.cls` + namespace
binding, which is why the API narrowed rather than grew. What remains of it is
phase 2 — fetching attribute values across the bridge — deliberately deferred,
since phase 1 made the gap loud and value semantics mean it can never be fully
transparent anyway.

## The problem

Workers are `fork()`ed from the embedding process. Fork duplicates only the
calling thread, so any lock another thread holds at that instant is inherited
already-held by a child that has no thread to release it. The child then
deadlocks on first contention — it does not crash, so nothing observes an exit
code. The caller's own deadline eventually fires and reports
`"Worker process became unresponsive"`, which names none of this.

Confirmed in production (#38) at kernel level: a hung worker showed
`State: S (sleeping)`, `Threads: 2`, and every thread parked on
`futex_wait_queue` with no voluntary context switches — a genuine lock wait,
not a slow call.

The host was a uvicorn/FastAPI server. That is not an unlucky configuration:
uvicorn is multi-threaded by construction (starlette's anyio threadpool, plus
uvloop's libuv threadpool when installed), and there is no "fork early" window
once it is serving. CPython agrees — 3.12 emits
`DeprecationWarning: This process is multi-threaded, use of fork() may lead to
deadlocks in the child`, and 3.14 moves multiprocessing's Linux default off
fork.

**The fix is structural: workers must not be forked from the embedding
process.** Which specific lock it was does not change that, so identifying it
is not on the critical path.

## Chosen approach

Fork from a broker process that is single-threaded by construction and never
grows threads, rather than from the host.

Two ways to get there were considered.

**Path A — make `Policy` picklable, then use stdlib `forkserver`.** More
serialization work, but almost no plumbing: `multiprocessing`'s forkserver
already owns the broker lifecycle and the fd passing. Handles arbitrary
policies. The known cost — children re-importing granted modules (#33 measured
0.3–1s) — is addressed by deriving `set_forkserver_preload()` from the policy's
own registered module names.

**Path B — a bespoke broker seeded at startup.** Forks a broker that inherits
the registration set in memory, passing only per-worker scalars (`timeout`,
`tick_limit`, `module_root`, `memory_limit` — all trivially picklable). Needs
almost none of the serialization work, but means owning fd passing over a unix
socket, broker lifecycle, crash recovery, and reaping — machinery `forkserver`
already ships.

**Decision: Path A.** Less code owned by sandtrap, more owned by CPython, and it
doesn't constrain policies to what was known at startup. The serialization work
is largely required for a spawn-fallback anyway.

## Sequencing decision: spawn first, forkserver second

`spawn` imposes the identical serialization requirement with none of the broker
lifecycle. Making spawn work first gives a conformance vehicle: if a policy
survives spawn and the process suite passes under it, forkserver is mechanical
afterward. Debugging policy serialization and broker lifecycle at the same time
is how this becomes a month of work.

The seam is `ProcessSandbox(..., start_method=...)`, defaulting to `"fork"`.
Both implementations coexist; the default flips only once conformance is green.

## What blocks pickling today

Four distinct classes, each needing different treatment. Verified against a
realistic policy:

| # | Obstacle | Failure |
|---|---|---|
| 1 | `__post_init__` builds four `_*_pred` local lambdas into `__dict__` | `Can't pickle local object '_make_predicate.<locals>.<lambda>'` |
| 2 | `_ModuleRegistration.obj` holds a live `ModuleType` | `TypeError: cannot pickle 'module' object` |
| 3 | `_reg_by_cls_id` / `_reg_by_module_id` keyed by `id()` | pickles "successfully" — and is wrong |
| 4 | Embedder callables: `policy.fn(lambda)`, callable `include`/`exclude`, live-object grants | `PicklingError` / `AttributeError` |

(1) and (3) are derived state: reconstruct, never serialize. (2) is
reconstruct-by-name. (4) is genuinely unbridgeable and must fail loudly.

**(3) is the dangerous one.** Ints pickle fine, so a naive `__getstate__`
carries the id-maps across intact and invalid — `id(math)` differs in the child,
`_find_registration_for` silently misses, and the policy quietly changes
behavior. A policy that pickles but decides differently is a security regression
wearing a bug's clothing. This is why conformance testing is a gate, not a
nicety.

Scale is favourable: nontainer's 41-grant stdlib preset uses zero callable
patterns, so the realistic case is all-string patterns that pickle once the
derived lambdas are excluded.

## Live-object grants: a silent bug, not a lost feature

`policy.module(live_obj, name=...)` cannot cross to a non-forked worker. That
reads like a capability loss until you check what it does today under
`isolation="process"`:

```
worker saw: 1  2          # sandboxed code bumped the counter twice
host object after:  0     # ...and the host never saw it
```

Fork hands the child a copy-on-write **snapshot**. Mutations never reach the
host. The "live" part has been false in process mode all along, silently.

So this work converts a silent wrong answer into a loud construction-time error.
Scope: `isolation="none"` keeps genuinely live objects and is untouched; only
process/kernel loses the grant, where it was already broken.

The supported mechanism is unaffected: live objects reached through
`rpc_handlers` + `RpcProxyMarker` stay parent-side and cross only as picklable
markers in the per-exec `ExecMsg`. Worker construction never sees them, so the
start method is irrelevant to them. Caveat for anyone migrating: attribute
*reads* don't cross the RPC bridge, only method calls — it is a different shape,
not a drop-in replacement.

## Work breakdown

Tracked here rather than as GitHub issues: the steps only make sense read end to
end, and two homes would drift. [#33](https://github.com/ashenfad/sandtrap/issues/33)
and [#38](https://github.com/ashenfad/sandtrap/issues/38) remain the issues this
work closes.

| # | Work | Depends on | Status |
|---|---|---|---|
| 1 | `start_method` seam, default `"fork"` | — | done |
| 2 | Registrations reconstruct derived predicates | — | done |
| 3 | Module grants cross by name; live-object grants raise | 2 | done |
| 4 | `Policy` rebuilds its `id()` indexes on unpickle | 2, 3 | done |
| 5 | `Policy.check_picklable()` | — | done |
| 6 | Spawn worker path: skip the fork-specific steps | 1–4 | done |
| 7 | Conformance suite | 6 | done; gap closed |
| 8 | forkserver context + policy-derived preload | 7 | done |
| 9 | Docs | — | todo |
| 10 | Refuse `fn` grants that pickle by value | 3 | done |
| 11 | RPC-backed policy registrations | 10 | todo — gates the default |

5 and 9 are parallelizable at any point. Steps 2–4 are roughly a day between
them; step 7 is where the time actually goes and should not be compressed.

### 1 — `start_method` seam

Add `start_method: Literal["fork", "spawn", "forkserver"] = "fork"` to
`ProcessSandbox`, replacing the hardcoded `multiprocessing.get_context("fork")`
in `_ensure_worker`. Non-fork values raise `NotImplementedError` until the rest
lands.

Experimental in the docstring — a seam for this work, not yet a supported knob.

**Acceptance:** default construction is exactly today's behavior and the existing
suite passes untouched; non-fork raises clearly.

### 2 — Registrations reconstruct derived predicates

`_ClsRegistration.__post_init__` and `_ModuleRegistration.__post_init__` build
four predicates into the instance `__dict__` via `_make_predicate`, which returns
local lambdas. Any registration then fails to pickle even when the user supplied
only strings:

```
_ClsRegistration(cls=Pt, name="Pt")
  -> AttributeError: Can't pickle local object '_make_predicate.<locals>.<lambda>'
```

Pure derived state — the source `include`/`exclude` patterns are already fields.
Add `__getstate__`/`__setstate__` that drop the four `_*_pred` entries and rebuild
by calling `__post_init__()`.

**Acceptance:** string and tuple-of-string patterns round-trip; rebuilt predicates
decide identically to the originals over a corpus of names, not merely exist; a
*callable* pattern still fails to pickle, and fails on the callable rather than on
the derived state.

### 3 — Module grants cross by name; live-object grants raise

`_ModuleRegistration.obj` holds a live `ModuleType`
(`TypeError: cannot pickle 'module' object`). Serialize as a `("module", name)`
marker and re-import via `importlib.import_module` on load.

The same field also accepts a live object (`policy.module(obj, name="x")`), which
cannot cross to a non-forked worker. Raise at dump time, pointing at
`rpc_handlers` + `RpcProxyMarker`.

**Acceptance:** module grants round-trip and resolve to the same modules;
live-object grants raise naming the registration and the migration path, including
that attribute reads don't cross the RPC bridge; a non-importable module raises
with its name rather than an opaque `ImportError`.

### 4 — `Policy` rebuilds its `id()` indexes

The dangerous one. `_reg_by_cls_id` / `_reg_by_module_id` are keyed by `id()`:

```
p.module(math)
p._reg_by_module_id  ->  {4334787040: 'math'}
```

Unlike every other blocker here, these **pickle without complaint** and arrive in
the child mapping addresses that mean nothing there. `_find_registration_for` then
silently misses and the policy quietly decides differently.

`__getstate__` drops both maps; `__setstate__` rebuilds from `self.classes` /
`self.modules`. Ordering constraint: this must run *after* module grants are
re-imported, since `id(reg.obj)` needs the reconstructed object.

**Acceptance:** `_find_registration_for` returns the same registration before and
after a round trip for every registered class and module; the test asserts the
rebuilt mapping by identity, and would fail if `__getstate__` carried the maps
through.

### 5 — `Policy.check_picklable()`

`pickle` reports the *first* problem, from deep inside the serializer, with no
idea which registration it came from. Discovering that at respawn time under load
in production is exactly how #38 got filed against the wrong mechanism.

One pass over `functions`, `classes`, and `modules` returning **every** problem
with the registration name, the obstacle kind, and the remedy. Four kinds to
distinguish: unpicklable callable, callable pattern, live-object grant,
unimportable class.

**Acceptance:** reports all problems, not the first; clean for a policy built from
nontainer's 41-grant stdlib preset (no false positives on the common case).

**Resolved:** `ProcessSandbox` calls it whenever `start_method != "fork"` and
raises `StPolicyNotPortable` at construction, listing every problem. Fork is
exempt — it inherits memory, so none of this applies, and checking there would
break every existing embedder.

**"Does it pickle" turned out to be the wrong test.** A module built at runtime
serializes happily and fails to load on the other side — the exact case a
pre-flight check exists to catch:

```
ghost module: dumps OK          <-- a shallow check says fine
  ...but loads ImportError      <-- false negative, where it matters most
```

So the check reasons about *importability*, via `find_spec`, not just
serializability. One edge: `find_spec` raises `ValueError` rather than returning
None for a synthetic module registered in `sys.modules` without a `__spec__` —
a pattern embedders do use — so both outcomes are treated the same.

Two tiers: named diagnoses first (live-object grant, unimportable module,
callable filter, live callable), then `pickle.dumps` as a fallback reporting its
own message for anything unclassified. The fallback is suppressed when a named
diagnosis already applies, so one root cause is reported once.

**Not detected:** a class defined in `__main__` crosses by reference and resolves
only if the child's `__main__` re-imports cleanly. That is the import-safety
requirement in `docs/process.md`, noted in the method's docstring rather than
guessed at. A fully faithful check would round-trip the policy through a spawned
process (~80ms) — worth an opt-in `deep=True` if anyone needs it, not worth
making the default.

### 6 — Spawn worker path

Smaller than expected: the spike ran a spawned worker end-to-end with no
`_worker_entry` changes at all. This step is about *skipping* the two mechanisms
that exist purely to undo fork inheritance.

**Descriptor neutralization.** `_open_file_descriptors()` enumerates the parent's
fd numbers and the child dup2s `/dev/null` over exactly those numbers. Correct
under fork; meaningless under spawn, where a collision with a descriptor the
spawned child made for itself would neutralize its own control channel. See the
caveat in the spike section — a forced collision could not be made to fail, so
this is a latent hazard rather than a demonstrated bug, but the step is pointless
under spawn either way.

**`_PARENT_CONNECTIONS`.** Under spawn these are pickled *into* the child rather
than inherited by it — duplicating endpoints instead of cleaning them up. Pass an
empty tuple.

**Acceptance:** non-fork methods skip both; `close_fds=True` is either a
documented no-op or an explicit rejection (decide which is less surprising); fork
behavior untouched; multiple live sandboxes under spawn pinned as a test.

**Resolved while implementing:**

- **`close_fds` is a documented no-op** under non-fork methods, not a rejection.
  A spawned child inherits no descriptors, so what the flag asks for already
  holds — and nontainer passes `close_fds=True` unconditionally under process
  isolation, so rejecting would break every non-fork sandbox for no benefit.
- **`_PARENT_CONNECTIONS` still registers** the new endpoint regardless of start
  method; only *passing* it to the child is fork-only. A later fork still
  inherits this endpoint and must close its copy, even if this worker didn't.
- **Non-fork methods require an import-safe `__main__`.** The child re-imports
  it, so module-level work in the host's entry point must sit behind
  `if __name__ == "__main__":`, and a host started as `python -c` or from a REPL
  has no importable `__main__` at all — it surfaces as
  `FileNotFoundError: <stdin>` on the worker's stderr, reported to the parent as
  a worker that died during initialisation. Servers are unaffected (an ASGI app
  is imported, not run as `__main__`), but it belongs in step 9's docs. Fork has
  no such requirement.
- **Memory really isn't inherited, and there's a test that proves it.** Patch a
  granted module after building the policy: a forked worker sees the patch, a
  spawned one re-imports and sees the pristine module. That also pins the
  semantics step 9 has to describe, and would catch a future refactor silently
  degrading spawn back to fork.

### 7 — Conformance

The gate. Everything else here is small; this is what decides whether any of it
can be trusted, and it is aimed squarely at step 4's silent-divergence risk.

**Layer one: parametrize the existing process suite over start method.** ~128
tests across `test_process_sandbox.py` (52), `test_isolation.py` (53),
`test_remote_fs.py` (13), `test_rpc.py` (10). `test_process_sandbox.py` already
has a `psandbox` fixture; parametrizing the fixtures over `("fork", "spawn")`
reuses all of it as a differential against the current worker. Lifting the
inline-constructed sandboxes to fixtures is the bulk of the mechanical work.

**Layer two: a policy-decision differential.** Build a policy with a
representative registration set (nontainer's stdlib preset is a good corpus —
recursive and non-recursive, dotted and bare patterns), then run the same
decisions through a fork-inherited worker and a spawned one, asserting identical
results: `is_attr_allowed` across modules/classes/instances/MRO-inherited
attributes, `is_import_allowed` across registered/submodule/recursive/excluded
paths, `resolve_module` / `resolve_module_member`, `needs_network` /
`needs_host_fs`.

Compare results, not absence of exceptions. Allow→deny is a bug; deny→allow is a
vulnerability.

**Acceptance:** green under both start methods; the differential fails loudly if
step 4's fix is reverted (worth confirming by temporarily reintroducing the bug);
CI runs both, and any sampling to control wall-clock is stated explicitly rather
than done silently.

**Results.** Implemented as a `--start-method` pytest option (`tests/conftest.py`)
rather than by lifting ~128 inline constructions to fixtures. An autouse fixture
patches `ProcessSandbox.__init__` to `setdefault` the method, so the *existing*
tests run unchanged and a difference in result is a difference in the worker
rather than in the test.

```
pytest tests                            791 passed, 16 skipped
pytest tests --start-method=spawn       767 passed, 40 skipped
pytest tests --start-method=forkserver  765 passed, 42 skipped
```

Fork is unchanged from baseline. The differential still fails loudly with step
4's bug reintroduced, naming the probes that flipped — `attr:string.digits`
`False -> True`, `reg:math` `'math' -> None`. CI should run all three.

**The coverage gap, stated rather than buried.** 24 of 807 tests are marked
`fork_only`, and the reason matters more than the count: only *two* of them are
about fork inheritance as a behaviour (inherited descriptors; a dynamically
created module grant, which is correctly refused now). The other 22 verify
child-side behaviour **by patching the parent and relying on fork to carry the
patch into the child** — a spawned worker re-imports pristine modules and never
sees it.

So parametrizing the suite buys less conformance than this document originally
assumed, and it is weakest exactly where it would be most valuable:
`TestFailClosed` (5) and the `apply_isolation` forwarding tests (6) cover whether
policy flags reach kernel isolation and whether degraded isolation fails closed.
Those are security-relevant, and they are currently unverified under non-fork
workers.

**Gap closed, and it had been hiding a bug.** `fork_only` was doing double duty
— marking both "this *behaviour* is fork-specific" and "this *test's technique*
needs fork" — and conflating those concealed a real defect: a **spawned** worker
that died during init was diagnosed as fork hostility, advising the embedder to
"construct the sandbox earlier" and set `ARROW_DEFAULT_MEMORY_POOL`, none of
which applies to a worker that inherited nothing. Exactly the misdiagnosis #37
existed to prevent, reintroduced by the new start methods, and invisible because
`TestForkUnsafeError` was marked fork-only.

Three changes closed it:

1. **`_init_death_error` takes the start method.** Fork hostility is diagnosed
   only for forked workers; others get advice that fits (its own setup crashed,
   traceback on stderr). Covered by `TestInitDeathClassification`, which tests
   the classification directly — no worker, so no start method.
2. **The fail-closed decision moved into `_verify_isolation`.** It reads a
   status and decides; it never touches the start method. Driving it through a
   real worker only added a requirement the code doesn't have — that the worker
   be *made* to report degraded, which needs fork's inherited memory to patch.
   `TestFailClosedDecision` exercises it directly.
3. **`IsolationStatus` carries `allow_network`, `allow_host_fs`, and `root`.**
   The forwarding tests now assert what the worker *reports* instead of
   intercepting what the parent passed, so they run under every start method.
   Useful beyond the tests: an embedder verifying isolation wants to know what
   the worker was built with, not only which mechanisms engaged.

19 markers remain, but no *behaviour* is unverified: 14 are end-to-end wiring
checks whose logic is now covered method-agnostically above, four are genuinely
about fork (inherited descriptors, fork's own defaults, a dynamic-module grant
that is now correctly refused), and one — `test_worker_init_failure_reported` —
is technique-bound with no direct equivalent, which is the honest residual.

**Forkserver does not survive a fork of the host.** Two tests build a fake host
by forking this process and starting a worker inside it; under forkserver the
inner start dies with `ChildProcessError: [Errno 10] No child processes` from
`forkserver.ensure_running()`. CPython's `multiprocessing/forkserver.py` keeps a
module-global `_forkserver_pid` and registers **no** after-fork hook, so a forked
child inherits a pid that is not its child and `os.waitpid` fails.

Marked `no_forkserver` (they pass under spawn, so `fork_only` would over-skip),
but the underlying constraint is a production concern, not a test artifact: the
pre-fork server model — gunicorn, `uvicorn --workers N` — forks worker processes
from a supervisor. If sandtrap starts a forkserver before that fork, every forked
server worker inherits broken state. Step 8 has to handle it, most likely by
registering an `os.register_at_fork(after_in_child=...)` hook that resets
multiprocessing's forkserver state so the child starts its own.

### 8 — forkserver + policy-derived preload

Spawn is the conformance vehicle, not the destination — it pays interpreter boot
plus a re-import of every granted module per worker (#33 measured ~0.3–1s).
forkserver gets the same safety at fork cost.

Derive `set_forkserver_preload()` from the policy's own registered module names so
the broker has the grant set warm and children skip the re-import. That is what
turns #33's cost objection into a non-issue.

Plus broker lifecycle: start once and early, handle broker death, and make sure a
dead broker doesn't produce the same unexplained-failure loop this work exists to
eliminate.

**Acceptance:** conformance green under `"forkserver"` too; worker start cost
measured against fork and spawn with preload on and off, with the numbers landing
in `docs/process.md`; a non-importable preload entry doesn't take the broker down.

**Results.** Preload is what makes forkserver affordable:

| start method | worker start + one exec |
|---|---|
| `fork` | 4.8 ms |
| `forkserver` + `preload_grants=True` | 5.5 ms |
| `forkserver`, sandtrap only (the default) | 16–18 ms |
| `forkserver`, nothing preloaded | 42.5 ms |
| `spawn` | 77.4 ms |

**Grants are not preloaded by default** — a review catch, confirmed
empirically. Preloading executes a module's import-time code *in the broker*:

```
60752 threads=2   <- parent
60784 threads=2   <- BROKER      (a grant that starts a thread on import)
```

A multi-threaded broker forks workers that can inherit a held lock, which is
exactly the hang this default exists to remove. Preloading sandtrap alone is
safe (we control it; verified import-inert) and recovers most of the cost;
grants are the embedder's, so only the embedder can vouch for them, and
`preload_grants=True` is where they say so.

Derived from the grants' real `__name__` rather than their registration names
(they can differ, and only the former is importable), skipping non-module
grants so an unresolvable import can't take the broker down. Applied additively:
the list is process-global and read once at broker start, so never drop an
embedder's own preload and never let one sandbox shrink another's.

**Forked hosts recover.** The `ChildProcessError` case is caught at our own
`start()` and retried once after dropping the inherited bookkeeping — reactive
rather than an `os.register_at_fork` hook, so it fires only when sandtrap is
actually affected. Deliberately not `ForkServer._stop()`: that `waitpid()`s a
process we aren't the parent of and `unlink()`s the *other* process's socket.
The lock is replaced rather than acquired, since a fork that interrupted a
broker operation would have left it held with no thread to release it.

Verified by disabling the recovery: the pre-fork test fails with exactly
`ChildProcessError: [Errno 10] No child processes`.

**Residual gap.** Reactive recovery cannot help if the inherited lock is itself
poisoned — `ensure_running` acquires it *before* the pid check, so that case
deadlocks rather than raising. It needs a fork racing a broker operation, which
is narrow, and `os.register_at_fork(after_in_child=_reset_inherited_forkserver)`
would close it if it ever shows up in practice.

**One test can't run under forkserver, for a new reason.**
`test_idle_worker_exits_while_later_worker_remains_busy` signals the host with
`os.kill(os.getppid(), ...)`, and a worker's parent is the **broker**. That is a
real semantic difference worth documenting; the scenario it guards — one
worker's *inherited* endpoint suppressing another's EOF — cannot arise where
nothing is inherited. Its sibling,
`test_idle_worker_exits_when_parent_process_disappears`, now passes under
forkserver and lost its marker.

### 9 — Docs

In `docs/process.md`, alongside "Fork safety": module grants re-import (fresh C
state — a benefit — but host-side monkeypatching is lost); `policy.fn` callables
cross by reference, so closures and bound methods don't cross; live-object grants
unsupported, with the counter example above showing why that's a fix rather than a
removal; `isolation="none"` untouched; `close_fds` a no-op under non-fork methods.

Update the "Fork safety" section, which currently points at #33 as "recovering
automatically is tracked in" and will need to become "here's how to select a start
method".

Fold in the #38 `/proc` diagnostic while there: for a hung worker, `wchan` across
`/proc/<pid>/task/*` showing `futex_wait_queue` on every thread is a strong signal
of this bug class rather than a slow call — and unlike `syscall`, `stack`, or a
`py-spy` attach, it is readable without `CAP_SYS_PTRACE`.

## Found by CI: imports after lockdown

Linux CI failed on a class of bug fork had been masking. `_apply_linux` applies
Landlock first — its own setup needs syscalls seccomp would block — and then
calls `seccomp.apply()`, which imports the backend. But Landlock allows **only**
the sandbox root (unlike Seatbelt, which permits system read-only paths), so:

```
PermissionError: [Errno 13] Permission denied:
  '.../site-packages/pyseccomp.py'
```

A forked worker inherited `pyseccomp` in `sys.modules`, making the later import
a dict hit that touched no files. A worker that starts fresh has to read it from
disk, after the lockdown. The ordering was load-bearing and only incidentally
satisfied.

Fixed by `seccomp.preload()` before Landlock, guarded by a portable test that
asserts the call order (the failure itself reproduces only on Linux with
Landlock present).

**The general hazard remains and is worth tracking separately:** under Linux
kernel isolation, *any* module imported after `apply_isolation` is unreadable.
The known remaining instance is `RpcProxyMarker(wrapper=...)`, whose dotted
path is imported in `_substitute_proxy_markers` at exec time — nontainer uses
`wrapper="nontainer.cache:RemoteCache"`. Under fork that module was inherited;
under forkserver it is not preloaded, since the parent doesn't know the wrapper
paths when the broker starts. Options: preload wrapper modules the embedder
declares up front, or allow Landlock read access to the Python installation the
way Seatbelt already does — the latter is a security-posture change and wants
its own decision.

## Spike: does the control `Connection` survive spawn?

Issue 6 carries the only real unknown, so it was spiked before committing to the
rest. See the results section below.

The concern: `RpcProxy.__reduce__` notes that pickling a `Connection` "would
fail (kernel mode blocks the resource-sharer bind syscall)". If that applied to
passing the control connection through `Process(args=...)`, Path A would be dead
on arrival.

Reading `multiprocessing.reduction.DupFd` suggests it does not: while a spawn is
in progress, `context.get_spawning_popen()` is non-None and the fd goes through
`popen_obj.duplicate_for_child()` — inherited directly via `spawnv_passfds` — so
the resource-sharer socket path is never taken. The `RpcProxy` note concerns
pickling a connection *outside* of spawning, from an already-isolated worker,
which is a different situation.

### Results

Spiked by shimming `sandtrap.process.sandbox.multiprocessing` so `get_context()`
returns the spawn context, then driving a real `ProcessSandbox` through it with a
bare `Policy()` (which already pickles). macOS / CPython 3.12.9.

| | Check | Result |
|---|---|---|
| A | `Connection` through `Process(args=)` under spawn | works |
| B | Same, with kernel isolation applied in the child first | works — `seatbelt=applied`, `degraded=False`, under **both** spawn and forkserver |
| C | Remaining `_worker_entry` args (`RemoteFSMarker`, `IsolatedFS`, fd tuple, mode/isolation/echo strings) | all pickle |
| D | End-to-end `ProcessSandbox` on spawn: `sb.exec("x = 1 + 1")` | `{'x': 2}`, no error |
| E | `close_fds=True`, and two live sandboxes at once | both work |

**The `RpcProxy` warning does not apply here, as suspected.** While a spawn is in
progress `context.get_spawning_popen()` is non-None, so
`multiprocessing.reduction.DupFd` routes the fd through
`popen_obj.duplicate_for_child()` — inherited directly via `spawnv_passfds`,
never touching the resource-sharer socket. The docstring's concern is about
pickling a connection *outside* of spawning, from an already-isolated worker.

**Conclusion: Path A is viable, and `Policy` pickling is the only blocker.**
Result D is the load-bearing one — a spawned worker runs end-to-end today with no
changes to `_worker_entry` at all, which means there is no hidden coupling to
fork inheritance in the worker path. That shrinks issue 6 considerably: it is
mostly about *skipping* fork-specific steps, not adding a parallel
implementation.

### Caveats

**Linux is unverified.** This was run on macOS, where kernel isolation is
Seatbelt; production is Linux (seccomp + Landlock). The risk is low and the
argument is structural: the worker's post-isolation syscall usage on the control
channel is identical between fork and spawn — only fd *delivery* differs, and
that happens at exec time in the parent, before any filter is installed. It
should still be confirmed on Linux before the default flips.

**`close_fds` under spawn is correct by accident.** `_open_file_descriptors()`
enumerates the *parent's* fd numbers and the child dup2s `/dev/null` over exactly
those numbers. Under fork that's right; under spawn the numbers are meaningless,
and one colliding with a descriptor the spawned child made for itself would have
it neutralize its own control channel. I tried to force that collision by
occupying fds 3–50 in the parent and could not make it fail — so this is a latent
hazard, not a demonstrated bug. Either way the whole step is pointless under
spawn (nothing is inherited), and issue 6 should skip it explicitly rather than
leave it working by luck.

**`_PARENT_CONNECTIONS` is likewise fork-only.** Under spawn those connections
are *pickled into* the child rather than inherited by it — duplicating endpoints
instead of cleaning them up. It worked in test E, but the mechanism exists purely
to undo fork inheritance, so non-fork start methods should pass an empty tuple.

### 10 — Refuse `fn` grants that pickle by value

Step 3 refuses live-object *module* grants. The same divergence walks through
the `fn` door unchallenged, because bound methods and callable instances pickle
**by value**:

```
policy.fn(svc.record)  ->  bound method -> same live object?  False
                           host saw the call?              []
policy.fn(svc)         ->  callable instance -> same object? False
                           host saw the call?              []
```

The worker calls a copy and the host never hears about it — worse than a
refusal, because it looks like it worked.

`_carries_host_state()` distinguishes callables that cross by *reference*
(module-level functions, builtins, classes, classmethods — the class crosses by
name) from those that carry an instance, unwrapping `functools.partial` so a
partial is only as bridgeable as what it wraps. Lambdas and closures are
deliberately not named by the guard: they fail during pickling anyway, with an
error that points at the actual lambda rather than at sandtrap.

**Status: done.** 795 / 771 / 769 passing under fork / spawn / forkserver.

### 11 — RPC-backed policy registrations

The workflow question this whole chain has been circling: **what can an embedder
no longer do?** Measured, not reasoned:

| Registration | Crosses to a non-forked worker? |
|---|---|
| module-level function, builtin, `partial` of one, staticmethod, classmethod | yes |
| lambda, closure over local state | no — rejected loudly |
| bound method of a live object, callable instance | no — rejected by step 10 |
| dynamically built module (`types.ModuleType`) | no — no importable name |
| callable `include=` / `exclude=` filter | no |
| live object as module | no |

Plus, beyond registrations: host-side monkeypatching of a granted module is
silently lost (the worker re-imports pristine); registrations depending on
inherited file descriptors break, which is what `close_fds=False` exists to
protect; and a host without an import-safe `__main__` can't start a worker at
all.

Every one of those is a registration whose value is a **live thing in this
process rather than an importable reference**. A non-forked worker can receive
exactly two things: data, and names it can import.

So the gap isn't serialization — it's that **the RPC bridge is narrower than the
policy surface**. Today it carries method calls on `RpcProxyMarker`s passed in
the per-exec namespace. Not policy-registered functions, not attribute reads,
not synthesized modules.

`policy.fn(closure)` could work under a non-forked worker by being *bridged*
instead of serialized: register it as an RPC target and hand the worker a proxy.
A synthesized module is a namespace of callables, which is the same shape.
Attribute reads are the harder half — the existing bridge doesn't carry them,
and `policy.module(live_obj)` is mostly *about* attribute access.

**This probably belongs before the default flips.** Otherwise
`isolation="process"` quietly stops supporting the integration style `policy.fn`
was built for — and that regression would land on embedders as "my callback
silently stopped being called", which is the failure mode this whole document
exists to stop shipping.
