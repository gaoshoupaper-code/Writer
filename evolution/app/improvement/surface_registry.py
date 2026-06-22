"""surface 注册表（Phase 6 T1.2，决策 D6 代码内置枚举）。

定义 surface 体系的「类型契约」：每个 surface_type 属于哪层（A/B/C）、
content 是什么形态（text/json/python）、归属哪个 scope、用什么校验器。

设计依据：设计文档 D6（代码内置枚举）+ surface 三层分层（A 纯文本 / B 参数 / C 代码）。

为什么是代码内置而非 DB：
  - surface_type 集合是「开放但低频变动」的——加一个新类型（如 pacing_policy）
    是 C 类级别的大改，本就该改代码 + 重新部署。
  - 编译期可查：repo/loader 引用不存在的 surface_type 会直接 AttributeError，
    而非运行时才发现。
  - 校验器（Validator）是代码逻辑，无法存在 DB 里。

三层语义（决策 D1 + 设计 surface 分层）：
  - A_TEXT（纯文本）：改它不改 State schema。prompt/skill_md/description。
    自由进化，A/B 安全。content_kind='text'。
  - B_PARAM（JSON 参数）：改它改行为，可能不改 schema。middleware_params/permissions。
    校验后可进化。content_kind='json'。
  - C_CODE（受限 Python）：改它改 State schema。stateful_middleware（带 state_schema
    的 middleware 类）。连带 schema 版本，回放锁 schema。content_kind='python'。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class SurfaceLayer(str, Enum):
    """surface 三层（按改动是否影响 State schema 分层）。"""

    A_TEXT = "a_text"        # 纯文本：prompt/skill/description，自由进化
    B_PARAM = "b_param"      # JSON 参数：middleware 参数/permissions，校验后进化
    C_CODE = "c_code"        # 受限 Python：带 state_schema 的 middleware，全闸 + 连带 schema


class ContentKind(str, Enum):
    """content 字段的载体形态（校验分发依据）。"""

    TEXT = "text"            # A 类：markdown/纯文本
    JSON = "json"            # B 类：结构化 JSON 参数
    PYTHON = "python"        # C 类：受限 Python 代码片段


# scope 合法值（surface 归属的 subagent）。
# 加新 subagent 是 C 类大改（改 meta/agent 编排），需同步扩这里。
SCOPE_META = "meta"
SCOPE_STORYBUILDING = "storybuilding"
SCOPE_DETAIL_OUTLINE = "detail-outline"
SCOPE_WRITING = "writing"
SCOPE_INTERVIEW = "interview"
SCOPE_GLOBAL = "global"      # 不属于特定 subagent 的全局 surface

VALID_SCOPES = frozenset({
    SCOPE_META, SCOPE_STORYBUILDING, SCOPE_DETAIL_OUTLINE,
    SCOPE_WRITING, SCOPE_INTERVIEW, SCOPE_GLOBAL,
})


# Validator 签名：(content, config) -> (passed: bool, errors: list[str])。
# T1.2 只定义类型，具体校验器在 T3.2（static_check 分发）实现并注入 REGISTRY。
ValidatorFn = Callable[[str, dict[str, Any]], tuple[bool, list[str]]]


def _text_validator(content: str, config: dict[str, Any]) -> tuple[bool, list[str]]:
    """A 类轻校验：非空 + 长度上限。具体实现见 static_check（T3.2），此处占位。"""
    from app.improvement.static_check import validate_text_surface
    return validate_text_surface(content, config)


def _json_validator(content: str, config: dict[str, Any]) -> tuple[bool, list[str]]:
    """B 类 JSON 校验：解析 + schema。具体见 static_check（T3.2），此处占位。"""
    from app.improvement.static_check import validate_json_surface
    return validate_json_surface(content, config)


def _python_validator(content: str, config: dict[str, Any]) -> tuple[bool, list[str]]:
    """C 类全闸：AST + C4 + C5 + 契约。具体见 static_check（T3.2），此处占位。"""
    from app.improvement.static_check import validate_python_surface
    return validate_python_surface(content, config)


@dataclass(frozen=True)
class SurfaceTypeDef:
    """单个 surface_type 的类型定义。"""

    surface_type: str         # prompt / skill / stateful_middleware / ...
    layer: SurfaceLayer       # A/B/C 三层之一
    content_kind: ContentKind  # text/json/python
    validator: ValidatorFn    # 该类型的 content 校验器
    description: str          # 这个 surface_type 是什么（文档/可读性）


# ── surface 类型注册表（加新 surface 类型：在此加一行）──────────────

REGISTRY: dict[str, SurfaceTypeDef] = {
    # ── A 类·纯文本（改它不改 schema）──
    "prompt": SurfaceTypeDef(
        surface_type="prompt", layer=SurfaceLayer.A_TEXT, content_kind=ContentKind.TEXT,
        validator=_text_validator,
        description="子代理的系统提示词（system prompt）",
    ),
    "skill": SurfaceTypeDef(
        surface_type="skill", layer=SurfaceLayer.A_TEXT, content_kind=ContentKind.TEXT,
        validator=_text_validator,
        description="SKILL.md 技能定义文本（content=正文，config 含路径）",
    ),
    "description": SurfaceTypeDef(
        surface_type="description", layer=SurfaceLayer.A_TEXT, content_kind=ContentKind.TEXT,
        validator=_text_validator,
        description="子代理的功能描述（供父代理选择委托目标）",
    ),
    # ── B 类·JSON 参数（改它改行为，可能不改 schema）──
    "middleware_params": SurfaceTypeDef(
        surface_type="middleware_params", layer=SurfaceLayer.B_PARAM, content_kind=ContentKind.JSON,
        validator=_json_validator,
        description="中间件参数（如 max_new_lines、context_file_paths）",
    ),
    "permissions": SurfaceTypeDef(
        surface_type="permissions", layer=SurfaceLayer.B_PARAM, content_kind=ContentKind.JSON,
        validator=_json_validator,
        description="文件系统权限规则（FilesystemPermission 列表）",
    ),
    # ── C 类·受限 Python（改它改 State schema，连带 schema 版本）──
    "stateful_middleware": SurfaceTypeDef(
        surface_type="stateful_middleware", layer=SurfaceLayer.C_CODE, content_kind=ContentKind.PYTHON,
        validator=_python_validator,
        description="带 state_schema 的 middleware 类定义（唯一能改 State schema 的 surface）",
    ),
}


# ── 查询 API（供 repo/proposer/static_check 用）──────────────────


def get_type_def(surface_type: str) -> SurfaceTypeDef:
    """取 surface_type 的定义。不存在抛 KeyError（编译期类型安全）。"""
    if surface_type not in REGISTRY:
        raise KeyError(
            f"未知 surface_type: {surface_type}。合法值: {sorted(REGISTRY.keys())}。"
            f" 加新类型需改 surface_registry.REGISTRY（决策 D6：代码内置枚举）。"
        )
    return REGISTRY[surface_type]


def get_layer(surface_type: str) -> SurfaceLayer:
    """取 surface_type 所属层（A/B/C）。"""
    return get_type_def(surface_type).layer


def get_content_kind(surface_type: str) -> ContentKind:
    """取 surface_type 的 content 形态。"""
    return get_type_def(surface_type).content_kind


def is_c_code(surface_type: str) -> bool:
    """是否为 C 类（改 State schema 的受限代码）。"""
    return get_layer(surface_type) == SurfaceLayer.C_CODE


def validate_scope(scope: str) -> None:
    """校验 scope 合法。不合法抛 ValueError。"""
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"未知 scope: {scope}。合法值: {sorted(VALID_SCOPES)}。"
            f" 加新 subagent 需同步扩 VALID_SCOPES（C 类大改）。"
        )


def list_types() -> list[str]:
    """列出所有合法 surface_type（供 API 展示/校验用）。"""
    return sorted(REGISTRY.keys())
