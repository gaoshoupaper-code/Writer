"""评估集加载逻辑。

评估集结构（D6）：
  evolution/data/evalset/
    └─ case-001/
        └─ demand.md        ← 完整四层 12 维 demand

加载：load_case_demand(case_id) 读对应 case 的 demand.md 内容。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.core.settings import settings

logger = logging.getLogger("evolution.common.evalset")


def evalset_root() -> Path:
    """评估集根目录。"""
    return settings._evolution_root / "data" / "evalset"


def list_cases() -> list[str]:
    """列出评估集所有 case id。"""
    root = evalset_root()
    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir() if d.is_dir() and (d / "demand.md").exists()
    )


def load_case_demand(case_id: str) -> str:
    """加载某 case 的 demand.md 内容。

    Args:
        case_id: case 标识（如 "case-001"）

    Returns:
        demand.md 全文

    Raises:
        FileNotFoundError: case 不存在
    """
    demand_path = evalset_root() / case_id / "demand.md"
    if not demand_path.exists():
        raise FileNotFoundError(
            f"评估集 case {case_id} 不存在：{demand_path}。"
            f"可用 case: {list_cases()}"
        )
    content = demand_path.read_text(encoding="utf-8")
    logger.info("加载评估集 case %s: %d 字符", case_id, len(content))
    return content


def case_exists(case_id: str) -> bool:
    """检查 case 是否存在。"""
    return (evalset_root() / case_id / "demand.md").exists()


# front-matter 的 title 字段解析（决策 D8/D-Q2）
# 格式：HTML 注释块内 `- title: <标题文本>`，与 status 同一 regex 风格。
_TITLE_RE = re.compile(r"^-\s*title:\s*(.+?)\s*$", re.MULTILINE)


def parse_title(demand_md: str, case_id: str = "") -> str:
    """从 demand.md front-matter 解析 title 字段；无则回退 case_id。

    front-matter 是 HTML 注释块（前 300 字符内），与 status 的解析范围一致。
    """
    head = demand_md[:300]
    m = _TITLE_RE.search(head)
    if m:
        return m.group(1).strip()
    return case_id


def load_case(case_id: str) -> tuple[str, str, str]:
    """加载 case：返回 (case_id, title, demand_md)。

    Raises:
        FileNotFoundError: case 不存在
    """
    demand_md = load_case_demand(case_id)
    title = parse_title(demand_md, case_id)
    return case_id, title, demand_md


def list_cases_with_title() -> list[dict[str, str]]:
    """列出所有 case（带 title）：[{case_id, title}]。"""
    result = []
    for case_id in list_cases():
        try:
            _, title, _ = load_case(case_id)
        except FileNotFoundError:
            title = case_id
        result.append({"case_id": case_id, "title": title})
    return result


__all__ = [
    "evalset_root",
    "list_cases",
    "load_case_demand",
    "case_exists",
    "parse_title",
    "load_case",
    "list_cases_with_title",
]
