"""静态检查 + 契约测试（Phase 4 T4.3，D10）。

proposer 生成的任意 Python（D4）在进 A/B 前必须过这关。
monitoring（优化端）做纯静态分析（AST + 正则），实例化检查由 backend
（执行端）的沙箱做（T4.2，沙箱在 backend 环境加载 harness 验证契约方法可调用）。

monitoring 侧静态检查内容：
  - 语法检查（AST 解析）
  - 结构检查：必须有 WriterHarness 子类
  - C4 危险模式扫描（os.system/subprocess/eval/exec/socket 等——防越权）
  - C5 危险硬编码扫描（无条件拒绝特定题材——防误伤）

实例化检查（加载 harness.py 调 build 方法验证返回类型）在 backend 沙箱做，
因为需要 backend 的基类环境（monitoring 是独立 venv，不能 import backend）。

返回 (passed: bool, errors: list[str])。

设计依据：设计文档 D10 + 第三道闸风险分析。
"""
from __future__ import annotations

import ast
import re
import logging

logger = logging.getLogger("monitoring.static_check")

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

