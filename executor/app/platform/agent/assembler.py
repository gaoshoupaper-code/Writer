"""assembler —— config JSON → agent 实例的核心逻辑（Phase 8，Task 3.1）。

实现 compose 执行层：
  - resolve_class：class_ref（类名）→ Python 类（D5a 包内约定）
  - instantiate_middleware：spec + ctx → middleware 实例（D13a/D14b ctx 注入）
  - build_middleware_list：config 的 processor 列表 → middleware 实例列表

assemble（harnesses/current/__init__.py）调用本模块完成配置驱动的组装。

class_ref 解析约定（D5a）：
  - "GoalMiddleware" → source_root/middleware/goal.py → 取包内模块 → 取类
  - 包内 middleware 用相对 import（from ..tools import），不能单独加载文件。
    因此 resolve_class 从「已加载的包模块」取子模块，而非单独 importlib 文件。
  - 零注册：新 .py 进了 source_root 就能解析，无需登记

ctx 注入约定（D14b）：
  - middleware 声明 _inject_from_ctx = ["workspace_path"] 类属性
  - assemble 实例化时从 ctx 取对应属性，注入 __init__
  - params 只存可调参数，运行时值由 ctx 注入（D13a）

设计依据：设计文档 D5a/D13a/D14b。
"""
from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware

if TYPE_CHECKING:
    from contracts.runtime_context import RuntimeContext

logger = logging.getLogger("writer.assembler")

# 类名 → 文件名：处理连续大写（MetaReadOnly → meta_readonly，不拆 RO）
_camel_re = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# 特殊映射：完整类名 → 实际文件名（去 .py），处理算法无法覆盖的命名差异
# 这些是类名和文件名不一致的特例（文件名是约定，类名是历史遗留）
_SNAKE_OVERRIDES: dict[str, str] = {
    "MetaReadOnlyMiddleware": "meta_readonly",       # 文件名 readonly（一个词），非 read_only
    "FilesystemPathGuardMiddleware": "path_guard",   # 文件名省了 Filesystem 前缀
}


def _to_snake(name: str) -> str:
    """CamelCase → snake_case。GoalMiddleware → goal（去 middleware 后缀）。

    处理连续大写 + 历史命名差异（via override 表）。
    """
    if name in _SNAKE_OVERRIDES:
        return _SNAKE_OVERRIDES[name]
    if name.endswith("Middleware"):
        name = name[: -len("Middleware")]
    snake = _camel_re.sub("_", name).lower()
    snake = re.sub(r"_+", "_", snake)
    return snake


# ── class_ref 解析（D5a）─────────────────────────────────────────


def resolve_class(class_name: str, package: ModuleType) -> type:
    """从已加载的包模块中按约定取 middleware 类（D5a）。

    包内 middleware 用相对 import（from ..tools import），必须从包模块取子模块，
    不能单独 importlib 加载文件（否则相对 import 失败）。

    约定：class_name → snake_case → package.middleware.{snake} → 取类

    Args:
        class_name: 类名（如 "GoalMiddleware"）
        package:    已加载的包模块（loader.load_package 加载的，含 submodule_search_locations）

    Returns:
        middleware 类（AgentMiddleware 子类）

    Raises:
        AttributeError: 包中找不到该 middleware 模块或类
    """
    snake = _to_snake(class_name)
    mod_name = f"{package.__name__}.middleware.{snake}"
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, class_name, None)
    if cls is None:
        raise AttributeError(
            f"{package.__name__}.middleware.{snake} 中找不到类 {class_name}"
            f"（D5a：类名必须 = 文件内的类定义名）"
        )
    return cls


# ── ctx 注入 + 实例化（D13a/D14b）────────────────────────────────


def _ctx_attr(ctx: "RuntimeContext", attr: str) -> Any:
    """从 ctx 取属性值。attr 可以是 ctx 的字段名或常用别名。"""
    # 直接属性
    val = getattr(ctx, attr, None)
    if val is not None:
        return val
    # 常用别名映射（workspace_root ↔ workspace_path）
    aliases = {
        "workspace_root": "workspace_path",
        "workspace_path": "workspace_path",
    }
    alias = aliases.get(attr)
    if alias:
        return getattr(ctx, alias, None)
    raise ValueError(f"ctx 中找不到属性 {attr!r}（_inject_from_ctx 声明）")


def instantiate_middleware(
    spec: dict,
    ctx: "RuntimeContext",
    package: ModuleType,
) -> AgentMiddleware:
    """根据 spec 实例化一个 middleware（D13a/D14b）。

    流程：
      1. resolve_class(spec["class"], package) → 类
      2. 读类的 _inject_from_ctx，从 ctx 取运行时值
      3. cls(**injected, **spec["params"]) → 实例

    Args:
        spec:    {class, params}
        ctx:     RuntimeContext（运行时值来源）
        package: 已加载的包模块（class_ref 解析用）

    Returns:
        middleware 实例
    """
    class_name = spec["class"]
    params = spec.get("params", {})

    cls = resolve_class(class_name, package)

    # 读 _inject_from_ctx 声明（D14b）
    inject_attrs: list[str] = getattr(cls, "_inject_from_ctx", [])

    # 从 ctx 取注入值
    injected: dict[str, Any] = {}
    for attr in inject_attrs:
        injected[attr] = _ctx_attr(ctx, attr)

    # 合并：params（可调参数）+ injected（运行时值，优先级高）
    kwargs = {**params, **injected}

    return cls(**kwargs)


def build_middleware_list(
    processors: list[dict],
    ctx: "RuntimeContext",
    package: ModuleType,
) -> list[AgentMiddleware]:
    """从 config 的 processor 列表构建 middleware 实例列表。

    保持 config 里的顺序（顺序 = 执行顺序，DeepAgents middleware list 语义）。

    Args:
        processors: config 的 processor 列表 [{hook, group, spec}, ...]
        ctx:        RuntimeContext
        package:    已加载的包模块

    Returns:
        middleware 实例列表（按 config 顺序）
    """
    result: list[AgentMiddleware] = []
    for proc in processors:
        mw = instantiate_middleware(proc["spec"], ctx, package)
        result.append(mw)
    return result


__all__ = [
    "resolve_class",
    "instantiate_middleware",
    "build_middleware_list",
]
