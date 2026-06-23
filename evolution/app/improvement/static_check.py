"""静态检查 + surface 校验（Phase 6 surface 体系）。

proposer 生成的 surface content 在进 A/B 前必须过校验。
evolution 做 AST 分析 + 正则扫描，按 surface_type 分发到 text/json/python 校验。

校验内容（按 surface 层，contracts.surface_types）：
  - A_TEXT（纯文本）：非空 + 长度上限（validate_text_surface）
  - B_PARAM（JSON 参数）：合法 JSON 解析（validate_json_surface）
  - C_CODE（受限 Python）：C4 危险模式 + C5 误伤 + C 类契约（validate_python_surface）

返回 (passed: bool, errors: list[str])。

设计依据：设计文档 D10 + surface 三层分层 + 第三道闸风险分析。
"""
from __future__ import annotations

import ast
import re
import logging

logger = logging.getLogger("evolution.static_check")

# 危险 import/调用模式（C4：防越权）
_DANGEROUS_PATTERNS = [
    (r"\bos\.system\b", "禁止 os.system（命令注入风险）"),
    (r"\bsubprocess\.", "禁止 subprocess（进程越权风险）"),
    (r"\beval\s*\(", "禁止 eval（代码注入风险）"),
    (r"\bexec\s*\(", "禁止 exec（代码注入风险）"),
    (r"\b__import__\s*\(", "禁止 __import__（绕过静态分析）"),
    (r"\bsocket\.", "禁止 socket（网络越权）"),
    (r"\bopen\s*\([^)]*['\"]w", "禁止文件写入（应通过 middleware 的 FilesystemBackend）"),
]

# 危险硬编码（C5：无条件覆盖生成倾向——D10 第三道闸防的误伤）
_HARDCODED_REJECT_PATTERNS = [
    # 如：middleware 里硬编码「拒绝写文艺向」
    (r"if.*文艺.*:\s*return.*error", "禁止硬编码拒绝特定题材（会误伤文艺向）"),
    (r"if.*慢热.*:\s*return.*error", "禁止硬编码拒绝特定节奏（会误伤慢热向）"),
]


def _get_name(node: ast.expr) -> str | None:
    """从 AST 节点提取名称（支持 Name / Attribute）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


# ── Phase 6：按 content_kind 分发的 surface 校验（T1.2 骨架，T3.2 实质化）──
# surface_registry 的 validator 字段延迟 import 这三个函数。T1.2 给基本实现
# 保证可跑通；T3.2（进化端改造）补强 schema 校验 / C 类契约检查。


def validate_text_surface(content: str, config: dict) -> tuple[bool, list[str]]:
    """A 类（纯文本）校验：非空 + 长度上限。

    A 类改它不改 State schema，是最低风险的 surface，只做基本可用性检查。
    """
    errors: list[str] = []
    if not content or not content.strip():
        errors.append("[A 类] content 为空")
        return False, errors
    # 长度上限：防 LLM 生成失控（单个 prompt/skill 不应超 50k 字符）
    max_len = 50000
    if len(content) > max_len:
        errors.append(f"[A 类] content 超长: {len(content)} > {max_len}")
    return len(errors) == 0, errors


def validate_json_surface(content: str, config: dict) -> tuple[bool, list[str]]:
    """B 类（JSON 参数）校验：合法 JSON 解析。

    B 类是结构化参数（middleware_params/permissions），核心保证可解析。
    字段级 schema 校验（如某 middleware 必须有某参数）在 T3.2 按需补。
    """
    import json
    errors: list[str] = []
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        errors.append(f"[B 类] JSON 解析失败: {exc}")
        return False, errors
    return True, errors


def validate_python_surface(content: str, config: dict) -> tuple[bool, list[str]]:
    """C 类（受限 Python）全闸：C4 危险模式 + C5 误伤 + C 类契约（带 state_schema 的 middleware）。

    C 类是唯一能改 State schema 的 surface（stateful_middleware）。校验三道闸：
      1. 语法（AST 解析）
      2. C4 危险模式（os/subprocess/eval/exec/socket/写文件）——复用 static_check 的扫描
      3. C5 误伤硬编码——复用 static_check 的扫描
      4. C 类契约：必须定义一个 AgentMiddleware 子类，且有 state_schema 属性

    与整体 harness 的 static_check 区别：整体 harness 要求 WriterHarness 子类，
    C 类片段要求的是单个 middleware 类（带 state_schema），结构检查不同。
    """
    errors: list[str] = []

    # ── 语法检查 ──
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return False, [f"语法错误: {exc}"]

    # ── C4/C5 危险模式扫描（复用 static_check 的模式列表）──
    for pattern, msg in _DANGEROUS_PATTERNS:
        if re.search(pattern, content):
            errors.append(f"[C4 危险模式] {msg}")
    for pattern, msg in _HARDCODED_REJECT_PATTERNS:
        if re.search(pattern, content):
            errors.append(f"[C5 误伤风险] {msg}")

    # ── C 类契约：定义带 state_schema 的 AgentMiddleware 子类 ──
    contract_errors = _check_c_middleware_contract(tree)
    errors.extend(contract_errors)

    return len(errors) == 0, errors


def _check_c_middleware_contract(tree: ast.Module) -> list[str]:
    """C 类 surface 契约检查：必须定义一个带 state_schema 属性的 middleware 类。

    契约要求（D3 + 设计接口契约）：
      - 定义至少一个类
      - 该类继承自 AgentMiddleware（或带 Middleware 后缀的基类）
      - 该类有 state_schema 属性（类级赋值）

    宽松匹配类名含 "Middleware" 或基类含 "AgentMiddleware"/"Middleware"，
    避免强依赖执行端的精确 import 路径（evolution 是独立 venv）。
    """
    errors: list[str] = []
    middleware_classes: list[ast.ClassDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # 基类名含 Middleware（AgentMiddleware 或自定义 XxxMiddleware）
        base_names = []
        for base in node.bases:
            name = _get_name(base)
            if name:
                base_names.append(name)
        is_mw = any("Middleware" in n for n in base_names)
        # 或类名本身含 Middleware（兜底）
        if not is_mw and "Middleware" in node.name:
            is_mw = True
        if is_mw:
            middleware_classes.append(node)

    if not middleware_classes:
        errors.append(
            "[C 类契约] 未定义 Middleware 子类（C 类 surface 必须定义一个继承 "
            "AgentMiddleware 或类名含 Middleware 的类）"
        )
        return errors

    # 检查至少一个 middleware 类有 state_schema 属性
    has_state_schema = False
    for cls in middleware_classes:
        for stmt in cls.body:
            # 类级赋值：state_schema = GoalState
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "state_schema":
                        has_state_schema = True
                        break
            # 带注解赋值：state_schema: type = GoalState
            if isinstance(stmt, ast.AnnAssign):
                t = stmt.target
                if isinstance(t, ast.Name) and t.id == "state_schema":
                    has_state_schema = True
                    break
    if not has_state_schema:
        errors.append(
            "[C 类契约] Middleware 类未定义 state_schema 属性（C 类必须通过 "
            "state_schema 声明改了哪些 State channel）"
        )
    return errors


# ── surface validator 注册表（surface_type → validator 函数）──────────────
#
# 替代原 surface_registry 里通过 SurfaceTypeDef.validator 字段的间接调用。
# contracts.surface_types 只含类型定义（零依赖），validator 实现留 evolution。
# pipeline/proposer 调 VALIDATOR_MAP[surface_type](content, config) 分发校验。

from contracts import surface_types as _st

VALIDATOR_MAP: dict[str, object] = {
    # A 类（纯文本）→ validate_text_surface
    **{
        t: validate_text_surface
        for t in (t for t in _st.REGISTRY if _st.get_layer(t) == _st.SurfaceLayer.A_TEXT)
    },
    # B 类（JSON）→ validate_json_surface
    **{
        t: validate_json_surface
        for t in (t for t in _st.REGISTRY if _st.get_layer(t) == _st.SurfaceLayer.B_PARAM)
    },
    # C 类（受限 Python）→ validate_python_surface
    **{
        t: validate_python_surface
        for t in (t for t in _st.REGISTRY if _st.get_layer(t) == _st.SurfaceLayer.C_CODE)
    },
}


def validate_surface(surface_type: str, content: str, config: dict | None = None) -> tuple[bool, list[str]]:
    """按 surface_type 分发到对应 validator。

    替代旧路径 type_def.validator(content, config)。
    未知 surface_type 抛 KeyError（contracts.get_type_def 会拦）。
    """
    # 先校验 type 合法（contracts 抛 KeyError）
    _st.get_type_def(surface_type)
    validator = VALIDATOR_MAP[surface_type]
    return validator(content, config or {})  # type: ignore[operator]


