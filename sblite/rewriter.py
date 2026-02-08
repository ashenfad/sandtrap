"""AST rewriter: validates and transforms Python AST for sandboxed execution."""

import ast
import sys
from typing import TypeVar, cast

from .errors import SbValidationError

_N = TypeVar("_N", bound=ast.AST)

# Names that cannot be assigned to, deleted, or declared global/nonlocal.
_BLOCKED_NAMES = frozenset({"exec", "eval", "compile", "__import__"})


class Rewriter(ast.NodeTransformer):
    """Fail-closed AST rewriter.

    Validates and transforms a Python AST to inject security gates.
    Any AST node without an explicit visit_X handler is rejected
    in generic_visit.
    """

    def __init__(self, *, wrapped_mode: bool = False) -> None:
        super().__init__()
        self._tmp_counter = 0
        self._wrapped_mode = wrapped_mode
        self._func_asts: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self._class_asts: list[ast.ClassDef] = []
        self._class_depth = 0
        self._func_depth = 0

    def _new_tmp(self) -> str:
        name = f"__sb_tmp_{self._tmp_counter}"
        self._tmp_counter += 1
        return name

    def _recurse(self, node: ast.AST) -> ast.AST:
        """Delegate to the default NodeTransformer recursion."""
        return super().generic_visit(node)

    def generic_visit(self, node: ast.AST) -> ast.AST:
        """Reject any AST node type not explicitly handled."""
        raise SbValidationError(
            f"Unsupported syntax: {type(node).__name__}",
            lineno=getattr(node, "lineno", None),
            col=getattr(node, "col_offset", None),
        )

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _check_name_store(self, name: str, node: ast.AST) -> None:
        """Block assignment/deletion of reserved names."""
        if name.startswith("__sb_"):
            raise SbValidationError(
                f"Cannot assign to reserved name '{name}'",
                lineno=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )
        if name in _BLOCKED_NAMES:
            raise SbValidationError(
                f"Cannot assign to '{name}'",
                lineno=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )

    def _check_name_del(self, name: str, node: ast.AST) -> None:
        """Block deletion of reserved names."""
        if name.startswith("__sb_"):
            raise SbValidationError(
                f"Cannot delete reserved name '{name}'",
                lineno=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )
        if name in _BLOCKED_NAMES:
            raise SbValidationError(
                f"Cannot delete '{name}'",
                lineno=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )

    def _check_assign_targets(self, target: ast.AST) -> None:
        """Recursively check assignment targets for blocked names."""
        if isinstance(target, ast.Name) and isinstance(
            target.ctx, (ast.Store, ast.Del)
        ):
            self._check_name_store(target.id, target)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._check_assign_targets(elt)

    # ------------------------------------------------------------------
    # Attribute gate helpers
    # ------------------------------------------------------------------

    def _has_attr_in_target(self, target: ast.AST) -> bool:
        """Check if a target tree contains any Attribute nodes."""
        if isinstance(target, ast.Attribute):
            return True
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(self._has_attr_in_target(e) for e in target.elts)
        return False

    def _make_getattr(self, obj: ast.expr, attr: str, loc: ast.AST) -> ast.Call:
        """Build __sb_getattr__(obj, attr) call node."""
        call = ast.Call(
            func=ast.Name(id="__sb_getattr__", ctx=ast.Load()),
            args=[obj, ast.Constant(value=attr)],
            keywords=[],
        )
        return ast.copy_location(call, loc)

    def _make_setattr(
        self, obj: ast.expr, attr: str, value: ast.expr, loc: ast.AST
    ) -> ast.Expr:
        """Build Expr(__sb_setattr__(obj, attr, value)) statement node."""
        call = ast.Call(
            func=ast.Name(id="__sb_setattr__", ctx=ast.Load()),
            args=[obj, ast.Constant(value=attr), value],
            keywords=[],
        )
        return ast.copy_location(ast.Expr(value=call), loc)

    def _make_delattr(self, obj: ast.expr, attr: str, loc: ast.AST) -> ast.Expr:
        """Build Expr(__sb_delattr__(obj, attr)) statement node."""
        call = ast.Call(
            func=ast.Name(id="__sb_delattr__", ctx=ast.Load()),
            args=[obj, ast.Constant(value=attr)],
            keywords=[],
        )
        return ast.copy_location(ast.Expr(value=call), loc)

    def _emit_target_assign(
        self,
        target: ast.AST,
        value: ast.expr,
        stmts: list[ast.stmt],
        loc: ast.AST,
    ) -> None:
        """Recursively emit assignment statements for a target, gating Attributes."""
        if isinstance(target, ast.Attribute):
            obj = self.visit(target.value)
            stmts.append(self._make_setattr(obj, target.attr, value, loc))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for i, elt in enumerate(target.elts):
                if isinstance(elt, ast.Starred):
                    raise SbValidationError(
                        "Starred unpacking with attribute targets not yet supported",
                        lineno=getattr(loc, "lineno", None),
                        col=getattr(loc, "col_offset", None),
                    )
                idx = ast.copy_location(
                    ast.Subscript(
                        value=value,
                        slice=ast.Constant(value=i),
                        ctx=ast.Load(),
                    ),
                    loc,
                )
                self._emit_target_assign(elt, idx, stmts, loc)
        else:
            # Name, Subscript, etc. — normal assignment
            visited = cast(ast.expr, self.visit(target))
            stmts.append(
                ast.copy_location(ast.Assign(targets=[visited], value=value), loc)
            )

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    visit_Module = _recurse
    visit_Expr = _recurse
    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | list[ast.stmt]:
        if isinstance(node.target, ast.Attribute) and node.value is not None:
            # obj.attr: annotation = value → __sb_setattr__(obj, 'attr', value)
            obj = self.visit(node.target.value)
            value = self.visit(node.value)
            return self._make_setattr(obj, node.target.attr, value, node)
        return self._recurse(node)
    visit_Return = _recurse
    visit_Raise = _recurse
    visit_Assert = _recurse
    visit_Pass = _recurse
    visit_Break = _recurse
    visit_Continue = _recurse

    def visit_Assign(self, node: ast.Assign) -> ast.AST | list[ast.stmt]:
        for target in node.targets:
            self._check_assign_targets(target)

        if not any(self._has_attr_in_target(t) for t in node.targets):
            return self._recurse(node)

        # At least one target involves attribute assignment — decompose.
        value = self.visit(node.value)
        stmts: list[ast.stmt] = []

        # Store value in temp if multiple targets or tuple unpacking
        needs_tmp = len(node.targets) > 1 or isinstance(
            node.targets[0], (ast.Tuple, ast.List)
        )
        if needs_tmp:
            tmp = self._new_tmp()
            stmts.append(
                ast.copy_location(
                    ast.Assign(
                        targets=[ast.Name(id=tmp, ctx=ast.Store())], value=value
                    ),
                    node,
                )
            )
            value_ref: ast.expr = ast.Name(id=tmp, ctx=ast.Load())
        else:
            value_ref = value

        for target in node.targets:
            self._emit_target_assign(target, value_ref, stmts, node)

        return stmts

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST | list[ast.stmt]:
        if not isinstance(node.target, ast.Attribute):
            return self._recurse(node)

        # obj.attr OP= value →
        #   __sb_tmp = obj
        #   __sb_setattr__(__sb_tmp, 'attr', __sb_getattr__(__sb_tmp, 'attr') OP value)
        obj = self.visit(node.target.value)
        value = self.visit(node.value)
        attr_name = node.target.attr

        stmts: list[ast.stmt] = []

        # Evaluate obj once into temp
        tmp = self._new_tmp()
        stmts.append(
            ast.copy_location(
                ast.Assign(
                    targets=[ast.Name(id=tmp, ctx=ast.Store())], value=obj
                ),
                node,
            )
        )
        tmp_ref = ast.Name(id=tmp, ctx=ast.Load())

        # __sb_getattr__(tmp, 'attr') OP value
        get_call = self._make_getattr(tmp_ref, attr_name, node)
        binop = ast.copy_location(
            ast.BinOp(left=get_call, op=node.op, right=value), node
        )

        # __sb_setattr__(tmp, 'attr', result)
        stmts.append(self._make_setattr(tmp_ref, attr_name, binop, node))

        return stmts

    def visit_Delete(self, node: ast.Delete) -> ast.AST | list[ast.stmt]:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._check_name_del(target.id, target)

        if not any(isinstance(t, ast.Attribute) for t in node.targets):
            return self._recurse(node)

        stmts: list[ast.stmt] = []
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                obj = self.visit(target.value)
                stmts.append(self._make_delattr(obj, target.attr, node))
            else:
                target = self.visit(target)
                stmts.append(
                    ast.copy_location(ast.Delete(targets=[target]), node)
                )

        return stmts

    def visit_Import(self, node: ast.Import) -> ast.AST | list[ast.stmt]:
        # import math → math = __sb_import__('math')
        # import math as m → m = __sb_import__('math', alias='m')
        # import a.b → a = __sb_import__('a.b')
        # import a.b as x → x = __sb_import__('a.b', alias='x')
        stmts: list[ast.stmt] = []
        for alias in node.names:
            keywords = []
            if alias.asname is not None:
                bind_name = alias.asname
                keywords.append(
                    ast.keyword(arg="alias", value=ast.Constant(value=alias.asname))
                )
            else:
                # import a.b binds 'a' (the top-level)
                bind_name = alias.name.split(".")[0]

            call = ast.Call(
                func=ast.Name(id="__sb_import__", ctx=ast.Load()),
                args=[ast.Constant(value=alias.name)],
                keywords=keywords,
            )
            assign = ast.Assign(
                targets=[ast.Name(id=bind_name, ctx=ast.Store())],
                value=call,
            )
            stmts.append(ast.copy_location(assign, node))

        if len(stmts) == 1:
            return stmts[0]
        return stmts

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | list[ast.stmt]:
        if node.names and any(alias.name == "*" for alias in node.names):
            raise SbValidationError(
                "Wildcard imports are not allowed",
                lineno=node.lineno,
                col=node.col_offset,
            )

        # from math import sqrt → sqrt = __sb_importfrom__('math', 'sqrt')
        # from math import sqrt as s → s = __sb_importfrom__('math', 'sqrt')
        # from .foo import bar → bar = __sb_importfrom__('foo', 'bar', _level=1)
        module_name = node.module or ""
        level = node.level or 0
        stmts: list[ast.stmt] = []
        for alias in node.names:
            bind_name = alias.asname if alias.asname else alias.name
            keywords = []
            if level > 0:
                keywords.append(
                    ast.keyword(arg="_level", value=ast.Constant(value=level))
                )
            call = ast.Call(
                func=ast.Name(id="__sb_importfrom__", ctx=ast.Load()),
                args=[
                    ast.Constant(value=module_name),
                    ast.Constant(value=alias.name),
                ],
                keywords=keywords,
            )
            assign = ast.Assign(
                targets=[ast.Name(id=bind_name, ctx=ast.Store())],
                value=call,
            )
            stmts.append(ast.copy_location(assign, node))

        if len(stmts) == 1:
            return stmts[0]
        return stmts

    def visit_Global(self, node: ast.Global) -> ast.AST:
        for name in node.names:
            if name.startswith("__sb_"):
                raise SbValidationError(
                    f"Cannot declare '{name}' as global",
                    lineno=node.lineno,
                    col=node.col_offset,
                )
        return self._recurse(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> ast.AST:
        for name in node.names:
            if name.startswith("__sb_"):
                raise SbValidationError(
                    f"Cannot declare '{name}' as nonlocal",
                    lineno=node.lineno,
                    col=node.col_offset,
                )
        return self._recurse(node)

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    def _prepend_checkpoint(self, node: _N) -> _N:
        """Recurse into node, then insert __sb_checkpoint__() into its body.

        Preserves docstrings by inserting after any leading string literal.
        """
        node = cast(_N, self._recurse(node))
        checkpoint = ast.copy_location(
            ast.Expr(
                value=ast.Call(
                    func=ast.Name(id="__sb_checkpoint__", ctx=ast.Load()),
                    args=[],
                    keywords=[],
                )
            ),
            node,
        )
        # Insert after docstring if present — all callers pass nodes with .body
        body: list[ast.stmt] = node.body  # type: ignore[attr-defined]
        insert_idx = 0
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            insert_idx = 1
        body.insert(insert_idx, checkpoint)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST | list[ast.stmt]:
        self._func_depth += 1
        try:
            node = self._prepend_checkpoint(node)
        finally:
            self._func_depth -= 1
        if not self._wrapped_mode or self._class_depth > 0:
            return node
        return self._wrap_defun(node)

    def visit_AsyncFunctionDef(
        self, node: ast.AsyncFunctionDef
    ) -> ast.AST | list[ast.stmt]:
        self._func_depth += 1
        try:
            node = self._prepend_checkpoint(node)
        finally:
            self._func_depth -= 1
        if not self._wrapped_mode or self._class_depth > 0:
            return node
        return self._wrap_defun(node)

    def _wrap_defun(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[ast.stmt]:
        """Wrap a function def with __sb_defun__ for wrapped mode."""
        import copy

        if self._func_depth > 0:
            # Inner function: embed source string (survives cross-turn activation)
            ast_ref = ast.Constant(value=ast.unparse(node))
        else:
            # Top-level function: index into _func_asts
            idx = len(self._func_asts)
            self._func_asts.append(copy.deepcopy(node))
            ast_ref = ast.Constant(value=idx)

        # name = __sb_defun__(name, name_ref, ast_ref)
        wrap_call = ast.Call(
            func=ast.Name(id="__sb_defun__", ctx=ast.Load()),
            args=[
                ast.Constant(value=node.name),
                ast.Name(id=node.name, ctx=ast.Load()),
                ast_ref,
            ],
            keywords=[],
        )
        wrap_assign = ast.Assign(
            targets=[ast.Name(id=node.name, ctx=ast.Store())],
            value=wrap_call,
        )
        ast.copy_location(wrap_assign, node)
        return [node, wrap_assign]

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST | list[ast.stmt]:
        self._class_depth += 1
        try:
            node = cast(ast.ClassDef, self._recurse(node))
        finally:
            self._class_depth -= 1
        if not self._wrapped_mode or self._class_depth > 0:
            return node
        return self._wrap_defclass(node)

    def _wrap_defclass(self, node: ast.ClassDef) -> list[ast.stmt]:
        """Wrap a class def with __sb_defclass__ for wrapped mode."""
        import copy

        from .wrappers import _extract_names

        idx = len(self._class_asts)
        self._class_asts.append(copy.deepcopy(node))

        # Capture decorator and base class name references for freezing.
        # These values must be available when recompiling from AST.
        ref_names = _extract_names(node.decorator_list + node.bases)

        keywords = [
            ast.keyword(
                arg=name,
                value=ast.Name(id=name, ctx=ast.Load()),
            )
            for name in sorted(ref_names)
        ]

        wrap_call = ast.Call(
            func=ast.Name(id="__sb_defclass__", ctx=ast.Load()),
            args=[
                ast.Constant(value=node.name),
                ast.Name(id=node.name, ctx=ast.Load()),
                ast.Constant(value=idx),
            ],
            keywords=keywords,
        )
        wrap_assign = ast.Assign(
            targets=[ast.Name(id=node.name, ctx=ast.Store())],
            value=wrap_call,
        )
        ast.copy_location(wrap_assign, node)
        return [node, wrap_assign]
    visit_Lambda = _recurse

    # ------------------------------------------------------------------
    # Control flow
    # ------------------------------------------------------------------

    visit_If = _recurse
    visit_IfExp = _recurse

    def visit_For(self, node: ast.For) -> ast.AST:
        if self._has_attr_in_target(node.target):
            raise SbValidationError(
                "Attribute targets in for loops are not supported",
                lineno=node.lineno,
                col=node.col_offset,
            )
        return self._prepend_checkpoint(node)

    def visit_While(self, node: ast.While) -> ast.AST:
        return self._prepend_checkpoint(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AST:
        if self._has_attr_in_target(node.target):
            raise SbValidationError(
                "Attribute targets in for loops are not supported",
                lineno=node.lineno,
                col=node.col_offset,
            )
        return self._prepend_checkpoint(node)

    def visit_With(self, node: ast.With) -> ast.AST:
        for item in node.items:
            if item.optional_vars is not None and self._has_attr_in_target(
                item.optional_vars
            ):
                raise SbValidationError(
                    "Attribute targets in with statements are not supported",
                    lineno=node.lineno,
                    col=node.col_offset,
                )
        return self._recurse(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST:
        for item in node.items:
            if item.optional_vars is not None and self._has_attr_in_target(
                item.optional_vars
            ):
                raise SbValidationError(
                    "Attribute targets in with statements are not supported",
                    lineno=node.lineno,
                    col=node.col_offset,
                )
        return self._recurse(node)
    visit_Try = _recurse

    # Python 3.11+: TryStar (try/except*)
    if sys.version_info >= (3, 11):
        visit_TryStar = _recurse

    # Python 3.10+: match/case
    visit_Match = _recurse

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    visit_BinOp = _recurse
    visit_UnaryOp = _recurse
    visit_BoolOp = _recurse
    visit_Compare = _recurse
    visit_Call = _recurse
    visit_Subscript = _recurse
    visit_Slice = _recurse
    visit_Starred = _recurse
    visit_NamedExpr = _recurse

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        # Visit the object sub-expression first
        node.value = self.visit(node.value)

        if isinstance(node.ctx, ast.Load):
            # obj.attr → __sb_getattr__(obj, 'attr')
            return self._make_getattr(node.value, node.attr, node)

        # Store/Del context — return with visited .value.
        # The parent Assign/Delete/AugAssign handles gate injection.
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if isinstance(node.ctx, ast.Load) and node.id.startswith("__sb_"):
            raise SbValidationError(
                f"Cannot reference reserved name '{node.id}'",
                lineno=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )
        if isinstance(node.ctx, ast.Store):
            self._check_name_store(node.id, node)
        elif isinstance(node.ctx, ast.Del):
            self._check_name_del(node.id, node)
        return self._recurse(node)

    visit_Constant = _recurse

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------

    visit_List = _recurse
    visit_Tuple = _recurse
    visit_Set = _recurse
    visit_Dict = _recurse
    visit_JoinedStr = _recurse
    visit_FormattedValue = _recurse

    # ------------------------------------------------------------------
    # Comprehensions
    # ------------------------------------------------------------------

    visit_ListComp = _recurse
    visit_SetComp = _recurse
    visit_DictComp = _recurse
    visit_GeneratorExp = _recurse

    # ------------------------------------------------------------------
    # Async / generators
    # ------------------------------------------------------------------

    visit_Await = _recurse
    visit_Yield = _recurse
    visit_YieldFrom = _recurse

    # ------------------------------------------------------------------
    # Pattern matching (3.10+)
    # ------------------------------------------------------------------

    visit_match_case = _recurse
    visit_MatchValue = _recurse
    visit_MatchSingleton = _recurse
    visit_MatchSequence = _recurse
    visit_MatchStar = _recurse
    visit_MatchMapping = _recurse
    visit_MatchClass = _recurse
    visit_MatchAs = _recurse
    visit_MatchOr = _recurse

    # ------------------------------------------------------------------
    # Helpers (reached by recursion into child nodes)
    # ------------------------------------------------------------------

    visit_ExceptHandler = _recurse
    def visit_comprehension(self, node: ast.comprehension) -> ast.AST:
        node = cast(ast.comprehension, self._recurse(node))
        # Inject checkpoint as an always-true filter: __sb_checkpoint__() or True
        checkpoint = ast.Call(
            func=ast.Name(id="__sb_checkpoint__", ctx=ast.Load()),
            args=[],
            keywords=[],
        )
        always_true = ast.BoolOp(
            op=ast.Or(),
            values=[checkpoint, ast.Constant(value=True)],
        )
        ast.copy_location(checkpoint, node.iter)
        ast.copy_location(always_true, node.iter)
        node.ifs.insert(0, always_true)
        return node
    visit_arguments = _recurse
    visit_arg = _recurse
    visit_keyword = _recurse
    visit_alias = _recurse
    visit_withitem = _recurse

    # ------------------------------------------------------------------
    # Operators and contexts (leaf nodes)
    # ------------------------------------------------------------------

    visit_Add = _recurse
    visit_Sub = _recurse
    visit_Mult = _recurse
    visit_Div = _recurse
    visit_FloorDiv = _recurse
    visit_Mod = _recurse
    visit_Pow = _recurse
    visit_LShift = _recurse
    visit_RShift = _recurse
    visit_BitOr = _recurse
    visit_BitXor = _recurse
    visit_BitAnd = _recurse
    visit_MatMult = _recurse
    visit_And = _recurse
    visit_Or = _recurse
    visit_Eq = _recurse
    visit_NotEq = _recurse
    visit_Lt = _recurse
    visit_LtE = _recurse
    visit_Gt = _recurse
    visit_GtE = _recurse
    visit_Is = _recurse
    visit_IsNot = _recurse
    visit_In = _recurse
    visit_NotIn = _recurse
    visit_Invert = _recurse
    visit_Not = _recurse
    visit_UAdd = _recurse
    visit_USub = _recurse
    visit_Load = _recurse
    visit_Store = _recurse
    visit_Del = _recurse

    # ------------------------------------------------------------------
    # Python 3.12+ type parameter nodes
    # ------------------------------------------------------------------

    if sys.version_info >= (3, 12):
        visit_TypeVar = _recurse
        visit_ParamSpec = _recurse
        visit_TypeVarTuple = _recurse
        visit_TypeAlias = _recurse
