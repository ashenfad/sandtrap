# Sandtrap Recommendations

## High Priority

### 1. Verify f-string Edge Cases Through Gates
**Verified.** Tests confirm f-strings with format specs (`!r:>10`), nested format
specs, `!r`/`!s`/`!a` conversions, and method calls all route through attribute
gates correctly.

### 2. Sharpen the Threat Model Boundary
**Done.** security.md now explicitly enumerates out-of-scope vectors.

### 3. Audit All Paths to `type.__call__`
**Verified.** Tests confirm: alias (`t = type; t(...)`), `__class__.__class__`,
and MRO traversal to `type` are all blocked.

## Medium Priority

### 5. Document the `bare except` Rewrite
**Done.** Already documented in security.md "What's blocked" section.

### 6. Improve Memory Limit Accuracy
Memory limit enforcement via peak RSS is process-wide, not per-sandbox. The
"headroom" semantics help but it's best-effort. Documented as a known limitation.

### 7. Cache Compiled Code for `activate()`
Deferred. Only matters if activate() is called many times with identical ASTs.
Not actionable without profiling evidence.

### 8. Verify Python 3.14 Free-Threaded Compatibility
ContextVar is thread-safe by design. Worth running the test suite with
PYTHON_GIL=0 once 3.14 is stable, but not actionable now.

## Low Priority

### 9. `find_refs()` Over-Estimation
Acknowledged as safe. Over-estimation means slightly more is pickled than
needed but no dependencies are missed.

### 10. Consider `tracemalloc` for Per-Sandbox Memory Tracking
Interesting but heavyweight. tracemalloc has significant overhead and doesn't
play well with C extensions. RSS approach is coarse but fast.
