"""评估集加载逻辑（数据闭环重构，决策 A1/A2）。

评估集分层结构：
  evolution/data/evalset/
    ├─ golden/                 ← 冻结基准（跨版本可比的公制尺）
    │   └─ case-001/
    │       └─ demand.md
    └─ growing/                ← 生产 promote 进来的真实 case
        └─ case-101/
            ├─ demand.md
            └─ reference.md    ← 编辑终稿（参考产出，决策 D10）

加载：load_case_demand(case_id, layer=...) 读对应 case 的 demand.md 内容。
layer 可省略——自动查 dataset_meta 表推导（向后兼容旧调用方）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.core.settings import settings

logger = logging.getLogger("evolution.common.evalset")

# 数据集层（决策 D1：简化两层 golden+growing）
LAYERS = ("golden", "growing")
DEFAULT_LAYER = "golden"


def evalset_root() -> Path:
    """评估集根目录。"""
    return settings._evolution_root / "data" / "evalset"


def layer_root(layer: str) -> Path:
    """某层的根目录（golden / growing）。"""
    return evalset_root() / layer


# ── layer 推导（向后兼容核心）────────────────────────────────


def resolve_layer(case_id: str) -> str | None:
    """从 dataset_meta 表推导 case 所在的 layer。

    供 layer 参数缺省时自动推导，保证旧调用方（tests/api.py）不传 layer 也能工作。
    命中多条或表无记录时回退到文件系统扫描。

    Returns:
        layer 名（golden/growing）；找不到返回 None。
    """
    try:
        import app.core.db as db

        rows = db.query_all(
            "SELECT layer FROM dataset_meta WHERE case_id = ? AND status = 'active'",
            (case_id,),
        )
        if rows:
            return rows[0]["layer"]
    except Exception:
        # DB 未就绪（如 init_db 前）静默回退到文件系统扫描
        pass

    # 回退：扫描文件系统
    for layer in LAYERS:
        if (layer_root(layer) / case_id / "demand.md").exists():
            return layer
    return None


def _ensure_layer(layer: str | None, case_id: str) -> str:
    """归一化 layer：None → 推导 → 默认 golden。"""
    if layer:
        return layer
    return resolve_layer(case_id) or DEFAULT_LAYER


# ── case 列举 / 加载 ────────────────────────────────────────


def list_cases(layer: str | None = None) -> list[str]:
    """列出某层（或全部）的 case id。

    Args:
        layer: golden / growing / None（None=所有层合并）
    """
    layers = [layer] if layer else LAYERS
    result: list[str] = []
    for ly in layers:
        root = layer_root(ly)
        if not root.exists():
            continue
        result.extend(
            d.name for d in root.iterdir() if d.is_dir() and (d / "demand.md").exists()
        )
    return sorted(result)


def load_case_demand(case_id: str, layer: str | None = None) -> str:
    """加载某 case 的 demand.md 内容。

    Args:
        case_id: case 标识（如 "case-001"）
        layer: golden / growing / None（None=自动推导，向后兼容）

    Returns:
        demand.md 全文

    Raises:
        FileNotFoundError: case 不存在
    """
    ly = _ensure_layer(layer, case_id)
    demand_path = layer_root(ly) / case_id / "demand.md"
    if not demand_path.exists():
        raise FileNotFoundError(
            f"评估集 case {case_id} 不存在：{demand_path}。"
            f"可用 case: {list_cases()}"
        )
    content = demand_path.read_text(encoding="utf-8")
    logger.info("加载评估集 case %s [%s]: %d 字符", case_id, ly, len(content))
    return content


def case_exists(case_id: str, layer: str | None = None) -> bool:
    """检查 case 是否存在（layer=None 时全层扫描）。"""
    if layer:
        return (layer_root(layer) / case_id / "demand.md").exists()
    return any(
        (layer_root(ly) / case_id / "demand.md").exists() for ly in LAYERS
    )


def case_layer(case_id: str) -> str | None:
    """查询 case 所在层（显式接口，给 tests/api 用）。等价 resolve_layer。"""
    return resolve_layer(case_id)


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


def load_case(case_id: str, layer: str | None = None) -> tuple[str, str, str]:
    """加载 case：返回 (case_id, title, demand_md)。

    Raises:
        FileNotFoundError: case 不存在
    """
    demand_md = load_case_demand(case_id, layer=layer)
    title = parse_title(demand_md, case_id)
    return case_id, title, demand_md


def list_cases_with_title(layer: str | None = None) -> list[dict[str, str]]:
    """列出所有 case（带 title）：[{case_id, title, layer}]。"""
    result = []
    for case_id in list_cases(layer=layer):
        try:
            _, title, _ = load_case(case_id)
            ly = resolve_layer(case_id) or DEFAULT_LAYER
        except FileNotFoundError:
            title = case_id
            ly = DEFAULT_LAYER
        result.append({"case_id": case_id, "title": title, "layer": ly})
    return result


def reference_path(case_id: str, layer: str | None = None) -> Path:
    """编辑终稿（参考产出，决策 D10）的文件路径。"""
    ly = _ensure_layer(layer, case_id)
    return layer_root(ly) / case_id / "reference.md"


def save_reference(case_id: str, content: str, layer: str | None = None) -> Path:
    """保存编辑终稿到 case 目录（promote 入 growing 时调用）。"""
    ly = _ensure_layer(layer, case_id)
    case_dir = layer_root(ly) / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "reference.md"
    path.write_text(content, encoding="utf-8")
    logger.info("保存参考产出 case %s [%s]: %d 字符", case_id, ly, len(content))
    return path


__all__ = [
    "LAYERS",
    "DEFAULT_LAYER",
    "evalset_root",
    "layer_root",
    "resolve_layer",
    "case_layer",
    "list_cases",
    "load_case_demand",
    "case_exists",
    "parse_title",
    "load_case",
    "list_cases_with_title",
    "reference_path",
    "save_reference",
]
