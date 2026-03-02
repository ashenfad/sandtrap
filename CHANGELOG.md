# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
