"""静态检查 + 契约测试（Phase 4 T4.3，D10）。

proposer 生成的任意 Python（D4）在进 A/B 前必须过这关。
evolution（优化端）做纯静态分析（AST + 正则），实例化检查由 backend
（执行端）的沙箱做（T4.2，沙箱在 backend 环境加载 harness 验证契约方法可调用）。

evolution 侧静态检查内容：
  - 语法检查（AST 解析）
  - 结构检查：必须有 WriterHarness 子类
  - C4 危险模式扫描（os.system/subprocess/eval/exec/socket 等——防越权）
  - C5 危险硬编码扫描（无条件拒绝特定题材——防误伤）

实例化检查（加载 harness.py 调 build 方法验证返回类型）在 backend 沙箱做，
因为需要 backend 的基类环境（evolution 是独立 venv，不能 import backend）。

返回 (passed: bool, errors: list[str])。

设计依据：设计文档 D10 + 第三道闸风险分析。
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


def static_check(code: str) -> tuple[bool, list[str]]:
    """对 proposer 生成的 harness 代码做静态检查 + 契约测试。

    Args:
        code: 完整的 harness.py 代码字符串

    Returns:
        (passed, errors)。passed=True 表示全过，errors 为失败原因列表。
    """
    errors: list[str] = []

    # ── 语法检查（AST 解析）──
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, [f"语法错误: {exc}"]

    # ── C4: 危险模式扫描（防越权）──
    for pattern, msg in _DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            errors.append(f"[C4 危险模式] {msg}")

    # ── C5: 危险硬编码扫描（防误伤，D10 第三道闸核心）──
    for pattern, msg in _HARDCODED_REJECT_PATTERNS:
        if re.search(pattern, code):
            errors.append(f"[C5 误伤风险] {msg}")

    # ── 结构检查：必须有 WriterHarness 子类 ──
    if not _has_writer_harness_subclass(tree):
        errors.append("未定义 WriterHarness 子类")

    return len(errors) == 0, errors


def _has_writer_harness_subclass(tree: ast.Module) -> bool:
    """AST 检查是否定义了 WriterHarness 的子类。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = _get_name(base)
            if name and "WriterHarness" in name:
                return True
    return False


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
    """C 类（受限 Python）全闸：复用 static_check + 契约（带 state_schema）。

    C 类是唯一能改 State schema 的 surface，过 C4（危险模式）+ C5（误伤）
    + 结构检查（定义 middleware 类 + 有 state_schema 属性）三道闸。
    T3.2 补 state_schema 契约的严格检查，此处先复用 static_check 兜底。
    """
    return static_check(content)

