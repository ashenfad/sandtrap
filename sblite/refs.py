"""Static analysis to find external name references in source code."""

import ast
from collections.abc import Mapping
from typing import Any


class _ModuleRefAnalyzer(ast.NodeVisitor):
    """Find names that module-level code reads from the namespace.

    Walks the original (pre-rewrite) AST and collects Name nodes in Load
    context that are not locally defined at module scope.  Nested scopes
    (functions, classes, lambdas) are treated as opaque — any free variable
    that bubbles up from a nested scope counts as a module-level read.
    """

    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.bound: set[str] = set()

    # --- Module-level binding ---

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Decorators are evaluated at module scope
        for dec in node.decorator_list:
            self.visit(dec)
        # The function name is bound at module scope
        self.bound.add(node.name)
        # Free variables inside the function body are module-level reads
        inner = _FunctionRefAnalyzer(node)
        self.loaded.update(inner.free)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Decorators and bases are evaluated at module scope
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        # The class name is bound at module scope
        self.bound.add(node.name)
        # Class body: names loaded at class scope that aren't bound there
        inner = _ClassRefAnalyzer()
        for stmt in node.body:
            inner.visit(stmt)
        self.loaded.update(inner.free)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name.split(".")[0]
            self.bound.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            self.bound.add(name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bound.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)

    # For/With targets bind names
    def visit_For(self, node: ast.For) -> None:
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # type: ignore[arg-type]

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars:
                self._bind_target(item.optional_vars)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)  # type: ignore[arg-type]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        pass  # global at module scope is a no-op

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        # walrus operator: target := value
        self._bind_target(node.target)
        self.visit(node.value)

    def _bind_target(self, target: ast.AST) -> None:
        """Extract bound names from an assignment target."""
        if isinstance(target, ast.Name):
            self.bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # x += 1 both reads and writes x
        if isinstance(node.target, ast.Name):
            self.loaded.add(node.target.id)
            self.bound.add(node.target.id)
        self.visit(node.value)
        # For attribute/subscript targets, generic_visit handles them
        if not isinstance(node.target, ast.Name):
            self.visit(node.target)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        inner = _FunctionRefAnalyzer(node)
        self.loaded.update(inner.free)

    @property
    def refs(self) -> set[str]:
        """Names that the code reads from the external namespace.

        Conservative: reports all names that are loaded anywhere at module
        scope, even if also locally bound.  The caller intersects with their
        state keys, so over-reporting is safe; under-reporting is not.
        """
        return set(self.loaded)


class _FunctionRefAnalyzer(ast.NodeVisitor):
    """Find free variables inside a function/lambda definition.

    Variables that are loaded but not bound within the function (or declared
    global/nonlocal) are "free" and must come from an enclosing scope.
    """

    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        self.bound: set[str] = set()
        self.loaded: set[str] = set()
        self.globals: set[str] = set()

        # Default values are evaluated in the enclosing scope
        args = node.args
        for default in args.defaults:
            self.visit(default)
        for default in args.kw_defaults:
            if default is not None:
                self.visit(default)

        # Parameters are bound
        for arg in args.args + args.posonlyargs + args.kwonlyargs:
            self.bound.add(arg.arg)
        if args.vararg:
            self.bound.add(args.vararg.arg)
        if args.kwarg:
            self.bound.add(args.kwarg.arg)

        # Visit body
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            for stmt in node.body:
                self.visit(stmt)

    @property
    def free(self) -> set[str]:
        return self.loaded - self.bound - self.globals

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bound.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            if node.id not in self.bound and node.id not in self.globals:
                self.loaded.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        # Nonlocal vars are free (they come from an enclosing scope)
        self.loaded.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        self.bound.add(node.name)
        inner = _FunctionRefAnalyzer(node)
        for name in inner.free:
            if name not in self.bound and name not in self.globals:
                self.loaded.add(name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        inner = _FunctionRefAnalyzer(node)
        for name in inner.free:
            if name not in self.bound and name not in self.globals:
                self.loaded.add(name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        self.bound.add(node.name)
        inner = _ClassRefAnalyzer()
        for stmt in node.body:
            inner.visit(stmt)
        for name in inner.free:
            if name not in self.bound and name not in self.globals:
                self.loaded.add(name)

    def visit_For(self, node: ast.For) -> None:
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # type: ignore[arg-type]

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars:
                self._bind_target(item.optional_vars)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)  # type: ignore[arg-type]

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            name = node.target.id
            if name not in self.bound and name not in self.globals:
                self.loaded.add(name)
            self.bound.add(name)
        self.visit(node.value)
        if not isinstance(node.target, ast.Name):
            self.visit(node.target)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind_target(node.target)
        self.visit(node.value)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, node.elt)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, node.elt)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, node.elt)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, node.key, node.value)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        *exprs: ast.AST,
    ) -> None:
        """Comprehensions have their own scope in Python 3."""
        comp_bound: set[str] = set()
        # First generator's iter is in the enclosing scope
        first = True
        for gen in generators:
            if first:
                self.visit(gen.iter)
                first = False
            else:
                # Subsequent iters can reference earlier comp vars,
                # but for simplicity we visit them here (any name not
                # in comp_bound will bubble up correctly)
                self._visit_in_scope(gen.iter, comp_bound)
            self._collect_target_names(gen.target, comp_bound)
            for if_clause in gen.ifs:
                self._visit_in_scope(if_clause, comp_bound)
        for expr in exprs:
            self._visit_in_scope(expr, comp_bound)

    def _visit_in_scope(self, node: ast.AST, scope_bound: set[str]) -> None:
        """Visit a node, treating scope_bound names as locally bound."""
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in scope_bound and child.id not in self.bound and child.id not in self.globals:
                    self.loaded.add(child.id)

    @staticmethod
    def _collect_target_names(target: ast.AST, names: set[str]) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _FunctionRefAnalyzer._collect_target_names(elt, names)
        elif isinstance(target, ast.Starred):
            _FunctionRefAnalyzer._collect_target_names(target.value, names)

    def _bind_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)


class _ClassRefAnalyzer(ast.NodeVisitor):
    """Find names loaded at class scope that aren't bound there.

    Class bodies have their own scope but don't create a closure scope
    for nested functions — so we just track bound/loaded at this level
    and bubble up free names.
    """

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.loaded: set[str] = set()

    @property
    def free(self) -> set[str]:
        return self.loaded - self.bound

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bound.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            if node.id not in self.bound:
                self.loaded.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        self.bound.add(node.name)
        inner = _FunctionRefAnalyzer(node)
        for name in inner.free:
            if name not in self.bound:
                self.loaded.add(name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        self.bound.add(node.name)
        inner = _ClassRefAnalyzer()
        for stmt in node.body:
            inner.visit(stmt)
        for name in inner.free:
            if name not in self.bound:
                self.loaded.add(name)


def _follow_transitive_deps(
    initial_refs: set[str],
    namespace: Mapping[str, Any],
) -> set[str]:
    """Expand refs by following SbFunction.global_refs transitively."""
    from .wrappers import SbFunction

    all_refs = set(initial_refs)
    queue = [name for name in initial_refs if name in namespace]
    visited: set[str] = set()

    while queue:
        name = queue.pop()
        if name in visited:
            continue
        visited.add(name)

        val = namespace.get(name)
        if isinstance(val, SbFunction):
            for dep in val.global_refs:
                all_refs.add(dep)
                if dep not in visited and dep in namespace:
                    queue.append(dep)

    return all_refs


def find_refs(
    source: str,
    *,
    namespace: Mapping[str, Any] | None = None,
) -> set[str]:
    """Find external names that source code references.

    Parses the source and statically determines which names are read from
    the namespace (i.e., not locally defined).  This enables callers to:

    1. Lazily deserialize only the state entries the code actually needs.
    2. Use the same set to check for mutations after execution.

    When *namespace* is provided, also follows transitive dependencies
    through ``SbFunction.global_refs``: if the source references ``A`` and
    ``A`` is an ``SbFunction`` whose ``global_refs`` include ``B``, then
    ``B`` is added to the result (and so on recursively).

    *namespace* can be any ``Mapping`` — including lazy containers that
    deserialize on ``get()``.  The BFS only touches values that are part
    of the dependency chain, so untouched entries stay serialized.

    Returns a set of name strings.  Builtins and ``__sb_*`` internal names
    are excluded.
    """
    tree = ast.parse(source)
    analyzer = _ModuleRefAnalyzer()
    for stmt in tree.body:
        analyzer.visit(stmt)
    refs = analyzer.refs
    # Exclude builtins and internal names — the sandbox provides these
    refs.discard("True")
    refs.discard("False")
    refs.discard("None")
    refs = {r for r in refs if not r.startswith("__sb_")}

    if namespace is not None:
        refs = _follow_transitive_deps(refs, namespace)

    return refs
