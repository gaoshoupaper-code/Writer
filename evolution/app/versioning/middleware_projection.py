"""Project versioned harness middleware assembly without executing its code."""
from __future__ import annotations

import ast
from typing import Any

HOOK_ORDER = (
    "before_agent",
    "before_model",
    "wrap_model_call",
    "after_model",
    "wrap_tool_call",
    "after_agent",
)

_RUNTIME_MIDDLEWARE: dict[str, dict[str, Any]] = {
    "TraceMiddleware": {
        "hooks": ["wrap_model_call", "wrap_tool_call"],
        "description": "记录模型与工具调用的 Trace 事件。",
    },
    "CreditsMiddleware": {
        "hooks": ["wrap_model_call", "wrap_tool_call"],
        "description": "在用户创作运行中执行积分预扣与结算。",
    },
    "ContextAssemblerMiddleware": {
        "hooks": ["before_model", "wrap_model_call"],
        "description": "在模型调用前装配该 Agent 所需的文件上下文。",
    },
    "ArtifactValidationMiddleware": {
        "hooks": ["after_model"],
        "description": "在 Agent 结束前校验必需产物是否已经生成。",
    },
}


def build_middleware_projection(
    package_sources: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Return the mounted middleware stack for every visible Agent."""
    catalog = _build_catalog(package_sources)
    root = package_sources.get("__init__.py", "")

    meta = _extract_stack(root, "assemble", "meta_middleware", group="meta")
    common = _extract_stack(root, "middleware_factory", "mw", group="base")

    storybuilding = _extract_stack(
        package_sources.get("subagents/storybuilding.py", ""),
        "build_storybuilding_deep_subagent",
        "storybuilding_middleware",
        base=common,
        group="agent",
    )
    detail_outline = _extract_stack(
        package_sources.get("subagents/detail_outline.py", ""),
        "build_detail_outline_deep_subagent",
        "project_middleware",
        base=common,
        group="agent",
    )
    writing = _extract_stack(
        package_sources.get("subagents/writing.py", ""),
        "build_writing_deep_subagent",
        "writing_middleware",
        base=common,
        group="agent",
    )

    storybuilding = _resolve_conditional_mount(
        storybuilding,
        "ContextAssemblerMiddleware",
        _call_supplies_keyword(root, "build_storybuilding_deep_subagent", "context_file_paths"),
    )

    factory_source = package_sources.get("subagents/factory.py", "")
    storybuilding = _extract_stack(
        factory_source, "build_deep_subagent", "mw", base=storybuilding, group="agent"
    )
    detail_outline = _extract_stack(
        factory_source, "build_deep_subagent", "mw", base=detail_outline, group="agent"
    )
    writing = _extract_stack(
        factory_source, "build_deep_subagent", "mw", base=writing, group="agent"
    )
    storybuilding = _resolve_conditional_mount(
        storybuilding,
        "ArtifactValidationMiddleware",
        _call_supplies_keyword(
            package_sources.get("subagents/storybuilding.py", ""),
            "build_deep_subagent",
            "artifact_paths",
        ),
    )
    detail_outline = _resolve_conditional_mount(
        detail_outline,
        "ArtifactValidationMiddleware",
        _call_supplies_keyword(
            package_sources.get("subagents/detail_outline.py", ""),
            "build_deep_subagent",
            "artifact_paths",
        ),
    )
    writing = _resolve_conditional_mount(
        writing,
        "ArtifactValidationMiddleware",
        _call_supplies_keyword(
            package_sources.get("subagents/writing.py", ""),
            "build_deep_subagent",
            "artifact_paths",
        ),
    )

    interview = _extract_stack(
        package_sources.get("subagents/interview.py", ""),
        "build_interview_deep_subagent",
        "middleware",
        base=common,
        group="agent",
    )
    # Credits is conditionally omitted by middleware_factory for interview.
    interview = [mw for mw in interview if mw["class_name"] != "CreditsMiddleware"]

    review_storybuilding = _extract_stack(
        package_sources.get("subagents/reviewers/storybuilding.py", ""),
        "build_storybuilding_reviewer",
        "review_middleware",
        base=common,
        group="agent",
    )
    review_detail_outline = _extract_stack(
        package_sources.get("subagents/reviewers/detail_outline.py", ""),
        "build_detail_outline_reviewer",
        "review_middleware",
        base=common,
        group="agent",
    )
    review_writing = _extract_stack(
        package_sources.get("subagents/reviewers/writing.py", ""),
        "build_writing_reviewer",
        "review_middleware",
        base=common,
        group="agent",
    )
    review_detail_outline = _resolve_conditional_mount(
        review_detail_outline,
        "ContextAssemblerMiddleware",
        _call_supplies_keyword(
            package_sources.get("subagents/detail_outline.py", ""),
            "build_detail_outline_reviewer",
            "context_file_paths",
        ),
    )
    review_writing = _resolve_conditional_mount(
        review_writing,
        "ContextAssemblerMiddleware",
        _call_supplies_keyword(
            package_sources.get("subagents/writing.py", ""),
            "build_writing_reviewer",
            "context_file_paths",
        ),
    )

    stacks = {
        "meta": meta,
        "general_purpose": [dict(mw) for mw in common],
        "interview": interview,
        "storybuilding": storybuilding,
        "storybuilding_review": review_storybuilding,
        "detail_outline": detail_outline,
        "detail_outline_review": review_detail_outline,
        "writing": writing,
        "writing_review": review_writing,
    }
    return {
        agent: [_enrich_mount(mount, catalog) for mount in mounts]
        for agent, mounts in stacks.items()
    }


def _build_catalog(package_sources: dict[str, str]) -> dict[str, dict[str, Any]]:
    catalog = {name: dict(info) for name, info in _RUNTIME_MIDDLEWARE.items()}
    for path, source in package_sources.items():
        if not path.startswith("middleware/") or not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        description = ast.get_docstring(tree)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Middleware"):
                continue
            hooks: list[str] = []
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                name = item.name[1:] if item.name.startswith("a") else item.name
                if name in HOOK_ORDER and name not in hooks:
                    hooks.append(name)
            catalog[node.name] = {
                "source_path": path,
                "description": description,
                "hooks": sorted(hooks, key=HOOK_ORDER.index),
            }
    return catalog


def _call_supplies_keyword(source: str, function_name: str, keyword_name: str) -> bool:
    if not source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        keyword.arg == keyword_name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _expr_name(node.func) == function_name
        for keyword in node.keywords
    )


def _resolve_conditional_mount(
    stack: list[dict[str, Any]],
    class_name: str,
    enabled: bool,
) -> list[dict[str, Any]]:
    if not enabled:
        return [mount for mount in stack if mount["class_name"] != class_name]
    return [
        {**mount, "optional": False} if mount["class_name"] == class_name else mount
        for mount in stack
    ]


def _extract_stack(
    source: str,
    function_name: str,
    variable_name: str,
    *,
    base: list[dict[str, Any]] | None = None,
    group: str,
) -> list[dict[str, Any]]:
    if not source:
        return [dict(mw) for mw in (base or [])]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [dict(mw) for mw in (base or [])]
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    if function is None:
        return [dict(mw) for mw in (base or [])]

    stack: list[dict[str, Any]] = []
    initialized = False

    def visit(statements: list[ast.stmt], conditional: bool = False) -> None:
        nonlocal stack, initialized
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
                value = statement.value
                if isinstance(target, ast.Name) and target.id == variable_name and value is not None:
                    stack = _initial_stack(value, base or [], group)
                    initialized = True
                    continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                    if call.func.value.id == variable_name:
                        if call.func.attr == "append" and call.args:
                            mount = _mount_from_expr(call.args[0], group, conditional)
                            if mount:
                                stack.append(mount)
                        elif call.func.attr == "insert" and len(call.args) >= 2:
                            mount = _mount_from_expr(call.args[1], group, conditional)
                            if mount:
                                index = _literal(call.args[0])
                                stack.insert(index if isinstance(index, int) else len(stack), mount)
                        elif call.func.attr == "extend" and call.args and base:
                            stack.extend(dict(mw) for mw in base)
            if isinstance(statement, ast.If):
                visit(statement.body, True)
                visit(statement.orelse, True)
            elif isinstance(statement, (ast.For, ast.While, ast.With, ast.Try)):
                for field in ("body", "orelse", "finalbody"):
                    nested = getattr(statement, field, None)
                    if nested:
                        visit(nested, conditional)

    visit(function.body)
    return stack if initialized else [dict(mw) for mw in (base or [])]


def _initial_stack(
    expression: ast.expr,
    base: list[dict[str, Any]],
    group: str,
) -> list[dict[str, Any]]:
    if isinstance(expression, ast.List):
        return [
            mount
            for item in expression.elts
            if (mount := _mount_from_expr(item, group, False)) is not None
        ]
    if isinstance(expression, ast.ListComp):
        excluded = {
            node.id
            for node in ast.walk(expression)
            if isinstance(node, ast.Name) and node.id.endswith("Middleware")
        }
        return [dict(mw) for mw in base if mw["class_name"] not in excluded]
    if isinstance(expression, ast.IfExp):
        return [dict(mw) for mw in base]
    if isinstance(expression, ast.Call) and _expr_name(expression.func) == "list":
        return [dict(mw) for mw in base]
    return []


def _mount_from_expr(
    expression: ast.expr,
    group: str,
    optional: bool,
) -> dict[str, Any] | None:
    call = expression if isinstance(expression, ast.Call) else None
    class_name = _class_name(call.func if call else expression)
    if not class_name:
        return None
    params: dict[str, Any] = {}
    if call:
        for keyword in call.keywords:
            if keyword.arg is None:
                continue
            value = _literal(keyword.value)
            if value is not None:
                params[keyword.arg] = value
    return {
        "class_name": class_name,
        "params": params,
        "group": "runtime" if class_name in {"TraceMiddleware", "CreditsMiddleware"} else group,
        "optional": optional,
    }


def _class_name(expression: ast.expr) -> str | None:
    name = _expr_name(expression)
    if not name:
        return None
    if name.endswith("Middleware"):
        return name
    if name.endswith("_middleware_cls"):
        return _snake_to_pascal(name.removesuffix("_cls"))
    if name.endswith("_middleware"):
        return _snake_to_pascal(name)
    return None


def _expr_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    return None


def _snake_to_pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _literal(expression: ast.expr) -> Any | None:
    try:
        return ast.literal_eval(expression)
    except (ValueError, TypeError):
        return None


def _enrich_mount(
    mount: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    info = catalog.get(mount["class_name"], {})
    hooks = info.get("hooks", [])
    return {
        **mount,
        # `hook` keeps already-installed desktop clients functional; new clients use
        # `hooks` so one mounted instance can appear in every lifecycle column it owns.
        "hook": hooks[0] if hooks else None,
        "hooks": hooks,
        "source_path": info.get("source_path"),
        "description": info.get("description"),
    }


__all__ = ["HOOK_ORDER", "build_middleware_projection"]
