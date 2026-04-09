# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
