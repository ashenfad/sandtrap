# Roadmap

Forward-looking notes on where sandtrap's isolation story is headed. These
are **not** implemented — this document captures the design so it isn't lost,
and so the [security model](security.md) can point here honestly instead of
overstating what exists today.

## Hardening kernel mode into an adversarial boundary

Today `isolation="kernel"` is **defense-in-depth**, not a boundary against
code that is actively trying to escape (see the [threat model](security.md#threat-model)).
Turning it into a real adversarial boundary is a *multi-part* effort. The
worker→host pickle channel is the headline hole, but closing it is **necessary,
not sufficient** — do not read "the allowlist shipped" as "kernel mode is now
adversarial-safe." The full list of prerequisites:

1. **Restricted deserialization on the host (this document's focus).** The
   parent unpickles the worker's `ResultMsg` (return value, namespace, error,
   prints) and `RpcCallMsg` (arguments to host handlers). A worker that has
   escaped the Python layer can forge a malicious pickle and execute in the
   host on unpickle. Closing this is the prerequisite described in detail below.
2. **The inner Python AST sandbox is not adversarial-safe.** Kernel restrictions
   only engage *after* code breaks out of the Python layer, and that layer is
   explicitly a "walled garden for cooperative code." An adversarial guarantee
   needs confidence in the inner layer too, or an argument that the kernel layer
   alone suffices regardless of what the Python layer lets through.
3. **Landlock is a single point of failure on Linux.** seccomp allows
   `open`/`openat` unconditionally; only Landlock path-restricts. Landlock is
   also the least-available mechanism (kernel 5.13+, frequently off in
   containers). The [fail-closed default](process.md#fail-closed-when-isolation-is-unavailable)
   now prevents *silent* single-layering, but a hardened story wants either a
   second filesystem-confinement layer or an explicit refusal to run kernel mode
   on Linux without Landlock.
4. **Residual syscall surface.** `ioctl` and `prctl` currently pass the seccomp
   allowlist unfiltered. Argument-filtering them would tighten the boundary.

Only when 1–4 are addressed does re-marketing kernel mode as adversarial
containment become honest.

## Restricted deserialization (prerequisite #1)

### The problem

Worker→host is the only untrusted direction (the worker runs the adversarial
code; everything the host sends is host-authored). Two message types carry
agent-controlled objects that the host unpickles:

- **`ResultMsg`** — the return value, the result namespace, `error`, and `prints`.
- **`RpcCallMsg`** — `args`/`kwargs` the agent passes into a host-side handler
  (cache writes, host-object method calls).

`filter_namespace` runs *worker-side* and only checks that values pickle; it does
nothing for host safety. So the host's `conn.recv()` in `ProcessSandbox` is the
exposure, and it's a small surface — the two `recv` sites in
`sandtrap/process/sandbox.py`.

### Why not just drop pickle

The stack's headline feature is "real Python objects across the boundary, no
JSON" — DataFrames, figures, Pydantic models, arbitrary user classes. Any wire
format that can carry those *is* a general object deserializer, with pickle's
gadget problem. JSON/msgpack close the hole only by discarding the feature. So
the two viable shapes are:

**Option A — restricted `Unpickler` (allowlist).** Subclass `pickle.Unpickler`
and override `find_class(module, name)` to raise unless `(module, name)` is on
an allowlist. pickle routes *every* global through `find_class`, including
`__reduce__` targets, so `(os, "system")` / `(builtins, "eval")` never resolve.
This genuinely closes the RCE. The cost is the allowlist itself:

- It must list the concrete classes agents legitimately return **and their
  reduce helpers** (numpy's `_reconstruct`, pandas' block reconstructors) —
  those run on unpickle.
- Coarse "allow all of `pandas.*`" is unsafe: the attacker writes the
  `(module, name)` strings, so a broad module grant is only as safe as the most
  gadget-like callable reachable in that module.
- It can't cover arbitrary user-defined return types without a registration
  mechanism.

**Option B — typed safe-subset.** Kernel mode accepts a restricted return
contract: primitives, containers, plus explicitly-registered reducers for known
types (DataFrame → arrow bytes, etc.). No pickle gadget surface at all. More
work, but it fits the stack's "capabilities degrade honestly" ethos — kernel
mode would advertise "returns restricted to registered-safe types" as a truthful
capability rather than pretending arbitrary objects are safe.

### Recommended design

Do **A**, scoped to kernel mode only, with the allowlist as a **caller-supplied
registry**:

- In-process (`isolation="none"`) has no separate trust boundary — keep plain
  pickle there. The restricted Unpickler wraps only the host's `recv` sites when
  `isolation="kernel"` (and covers `ResultMsg.error`, since a crafted exception's
  `__reduce__` is a live vector).
- sandtrap exposes the hook and ships safe defaults (stdlib containers, the
  numpy/pandas/pydantic reconstructors). The *caller* extends the allowlist for
  its own return types — the same way host objects are already registered.

### Layering with agex (the typed-contract seed)

agex has something sandtrap doesn't: a **typed return contract** (the `@task`
signature, validated with Pydantic). That type *is* the allowlist for the return
value. The clean division of labor:

- **sandtrap** owns the mechanism: a restricted `Unpickler` that takes a
  caller-supplied allowlist/registry. It stays "a namespace of stuff" and only
  accepts a policy.
- **agex** owns the policy: it seeds the allowlist from the task's return type
  (and any registered host types), because it's the layer that knows what a given
  task is contractually allowed to return.

This keeps the honesty seam clean — sandtrap doesn't pretend to know what's safe;
the layer with the type information supplies it.

### Status / trigger

Demand-gated. There are no current users of kernel mode against genuinely
adversarial code, so building this now would be speculative maintenance on a
solo-maintained stack. The design lives here so that when a real
adversarial-containment user appears, the work is a couple of `recv` sites plus
a registry — not a rediscovery.
