"""class_ref —— middleware 类名 → 源码文件路径解析（要素展示用）。

复制自 executor 端 `executor/app/platform/agent/assembler.py` 的 _to_snake +
_SNAKE_OVERRIDES 逻辑。evolution 端不跨服务 import executor，独立维护一份。

用途：要素展示端点把 config 里的 processor.spec.class（如 "GoalMiddleware"）
解析为相对 harness 包根的源码路径（如 "middleware/goal.py"），供前端懒加载
源码时直接调用 GET /snapshots/{version}/source?path=...。

注意：与 executor 端 assembler._to_snake 保持同步——若 executor 端新增
override 映射，这里也要同步。
"""
from __future__ import annotations

import re

# 类名 → 文件名：处理连续大写（MetaReadOnly → meta_readonly，不拆 RO）
_camel_re = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# 特殊映射：完整类名 → 实际文件名（去 .py），处理算法无法覆盖的命名差异
# 与 executor 端 assembler._SNAKE_OVERRIDES 保持同步
_SNAKE_OVERRIDES: dict[str, str] = {
    "MetaReadOnlyMiddleware": "meta_readonly",       # 文件名 readonly（一个词），非 read_only
    "FilesystemPathGuardMiddleware": "path_guard",   # 文件名省了 Filesystem 前缀
}


def class_to_snake(class_name: str) -> str:
    """CamelCase 类名 → snake_case 模块名（去 Middleware 后缀）。

    与 executor assembler._to_snake 同构：
      GoalMiddleware → goal
      MetaReadOnlyMiddleware → meta_readonly（override）
      RevisionLimitMiddleware → revision_limit
    """
    if class_name in _SNAKE_OVERRIDES:
        return _SNAKE_OVERRIDES[class_name]
    name = class_name
    if name.endswith("Middleware"):
        name = name[: -len("Middleware")]
    snake = _camel_re.sub("_", name).lower()
    snake = re.sub(r"_+", "_", snake)
    return snake


def class_to_source_path(class_name: str) -> str:
    """middleware 类名 → 相对包根的源码路径。

    GoalMiddleware → "middleware/goal.py"
    """
    return f"middleware/{class_to_snake(class_name)}.py"


__all__ = ["class_to_snake", "class_to_source_path"]
