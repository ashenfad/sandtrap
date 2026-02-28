# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.6] - 2026-02-28

### Fixed
- **ProcessSandbox worker respawn**: Dead workers now raise RuntimeError instead of silently re-forking, preventing deadlocks when threads are running
- **Bare assert in sandbox.py**: Replaced with proper RuntimeError guard

### Changed
- **Bare except: rewrite**: Documented rationale in security.md and rewriter docstring
- **monkeyfs dependency**: Pinned to <0.2.0
