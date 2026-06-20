"""回放测试集管理（Phase 3 T3.2）。

职责：
  - replay_test_sets 表的 CRUD（A/B 回放的标准化创作需求集）
  - COIG-Writer 玄幻子集导入（可选，需 datasets 库）

测试集结构：[{request: 创作需求, genre: 品类}, ...]
A/B 回放（experiment.py）用测试集的 request 作为创作需求，跑 production vs candidate。

设计依据：设计文档 D（半调研半自生成）+ COIG-Writer 核实结论。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.db as db

logger = logging.getLogger("monitoring.replay")


def list_test_sets() -> list[dict[str, Any]]:
    """列出所有回放测试集。"""
    rows = db.query_all("SELECT * FROM replay_test_sets ORDER BY id DESC")
    result = []
    for r in rows:
        item = dict(r)
        item["prompts"] = json.loads(r["prompts_json"]) if r["prompts_json"] else []
        item["item_count"] = len(item["prompts"])
        result.append(item)
    return result


def get_test_set(test_set_id: int) -> dict[str, Any] | None:
    """取单个测试集（含 prompts 解析）。"""
    row = db.query_one("SELECT * FROM replay_test_sets WHERE id=?", (test_set_id,))
    if row is None:
        return None
    item = dict(row)
    item["prompts"] = json.loads(row["prompts_json"]) if row["prompts_json"] else []
    return item


def create_test_set(name: str, prompts: list[dict[str, str]], description: str = "") -> dict[str, Any]:
    """创建回放测试集。

    Args:
        name: 测试集名（唯一）
        prompts: [{request: 创作需求, genre: 品类}, ...]
        description: 描述
    """
    existing = db.query_one("SELECT id FROM replay_test_sets WHERE name=?", (name,))
    if existing:
        raise ValueError(f"测试集已存在: {name}")
    now = datetime.now(UTC).isoformat()
    cur = db.execute(
        """INSERT INTO replay_test_sets (name, description, prompts_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (name, description, json.dumps(prompts, ensure_ascii=False), now),
    )
    result = get_test_set(cur.lastrowid)  # type: ignore[return-value]
    if result is not None:
        result["item_count"] = len(prompts)
    return result  # type: ignore[return-value]


def delete_test_set(test_set_id: int) -> bool:
    """删除测试集。"""
    cur = db.execute("DELETE FROM replay_test_sets WHERE id=?", (test_set_id,))
    return cur.rowcount > 0


# ── COIG-Writer 玄幻子集导入 ────────────────────────────────


def import_coig_xianxia(name: str = "coig-xianxia-replay", max_items: int = 50) -> dict[str, Any]:
    """从 COIG-Writer 数据集导入玄幻（仙侠）子集作为回放测试集。

    依赖 datasets 库 + 联网。失败则抛出明确错误（调用方可降级用手动测试集）。

    COIG-Writer 结构：prompt / thought / output 三元组，含仙侠品类。
    这里只取 prompt（创作需求）作为回放的 request，output 不用（量级不匹配）。
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "导入 COIG-Writer 需要 datasets 库：pip install datasets"
        ) from exc

    logger.info("加载 COIG-Writer 数据集...")
    ds = load_dataset("m-a-p/COIG-Writer", split="train")

    # COIG-Writer 字段：prompt / thought / output（可能含 genre 标记）
    # 仙侠子集筛选：prompt 或 output 含仙侠/玄幻/修真等关键词
    xianxia_keywords = {"仙侠", "玄幻", "修真", "修仙", "炼气", "金丹", "元婴"}
    prompts: list[dict[str, str]] = []
    for row in ds:
        prompt_text = str(row.get("prompt", "")).strip()
        output_text = str(row.get("output", "")).strip()
        combined = prompt_text + output_text
        # 匹配玄幻/仙侠品类
        if not any(kw in combined for kw in xianxia_keywords):
            continue
        if not prompt_text:
            continue
        prompts.append({"request": prompt_text, "genre": "玄幻"})
        if len(prompts) >= max_items:
            break

    if not prompts:
        raise RuntimeError("COIG-Writer 未匹配到玄幻/仙侠样本（可能数据结构变化）")

    logger.info("COIG-Writer 匹配到 %d 条玄幻样本", len(prompts))
    return create_test_set(
        name=name, prompts=prompts,
        description=f"COIG-Writer 玄幻/仙侠子集，{len(prompts)} 条创作需求",
    )


# ── 默认测试集（无 COIG-Writer 时的兜底）──


def ensure_default_test_set() -> dict[str, Any]:
    """确保存在默认玄幻回放测试集（无则用内置样本创建）。

    内置样本是几个典型玄幻创作需求（金手指/升级/打脸套路场景），
    供 A/B 回放在没有 COIG-Writer 时也能跑。
    """
    existing = db.query_one("SELECT id FROM replay_test_sets WHERE name='default-xianxia'")
    if existing:
        return get_test_set(existing["id"])  # type: ignore[return-value]

    default_prompts = [
        {
            "request": "写一部玄幻小说：主角获得上古传承金手指，从废柴逆袭，打脸曾经欺辱他的家族。要爽点密集，升级流。",
            "genre": "玄幻",
        },
        {
            "request": "修仙题材：凡人少年偶得仙缘，踏上修真之路。强调境界突破的爽感和宗门斗争。",
            "genre": "玄幻",
        },
        {
            "request": "玄幻系统文：主角绑定升级系统，完成任务获奖励。节奏要快，每章有爽点钩子。",
            "genre": "玄幻",
        },
    ]
    return create_test_set(
        name="default-xianxia", prompts=default_prompts,
        description="内置默认玄幻回放测试集（3 条典型套路场景）",
    )
