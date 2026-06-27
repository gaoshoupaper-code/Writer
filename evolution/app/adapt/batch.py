"""batch —— 固定测试集管理（Phase 8，Task 5.2，决策 A2a）。

人工构造的代表性写作需求，作为 adapt 的固定 batch。
每个 batch item = 一个生成请求的输入（genre + premise + 需求描述）。

batch 是 seesaw 约束的前提（有基准才能判退化，决策 A7b）。
轻档下 B=1-2（决策 A4），少量代表性样本。

batch 存储在 JSON 文件（evolution/data/adapt_batch.json），人工维护。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("evolution.adapt.batch")

# 默认 batch 文件路径（evolution/data/adapt_batch.json）
_BATCH_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "adapt_batch.json"


def default_batch() -> list[dict[str, Any]]:
    """返回默认 batch（内建，当 batch 文件不存在时用）。

    轻档 B=2：两个代表性题材组合，覆盖不同失败模式。
    """
    return [
        {
            "id": "batch-1",
            "genre": "玄幻",
            "premise": "一个被家族放弃的少年，意外获得上古传承，踏上复仇与成长之路",
            "title": "adapt-batch-1-玄幻复仇",
        },
        {
            "id": "batch-2",
            "genre": "都市",
            "premise": "职场新人面对公司内部权力斗争，在理想与现实间挣扎",
            "title": "adapt-batch-2-都市职场",
        },
    ]


def load_batch(path: Path | None = None) -> list[dict[str, Any]]:
    """加载固定测试集。

    优先从 batch 文件读（人工维护的代表样本）。文件不存在则用 default_batch。

    Args:
        path: batch 文件路径（None = 默认 _BATCH_FILE）

    Returns:
        batch item 列表，每个 = {id, genre, premise, title}
    """
    batch_path = path or _BATCH_FILE
    if batch_path.exists():
        try:
            data = json.loads(batch_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                logger.info("加载 batch 文件: %s (%d items)", batch_path, len(data))
                return data
        except Exception:
            logger.warning("batch 文件解析失败，用默认 batch: %s", batch_path, exc_info=True)

    logger.info("用默认 batch（%d items），batch 文件: %s", 2, batch_path)
    return default_batch()


def save_batch(batch: list[dict[str, Any]], path: Path | None = None) -> None:
    """保存 batch 到文件（人工编辑后持久化）。"""
    batch_path = path or _BATCH_FILE
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    batch_path.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("batch 已保存: %s (%d items)", batch_path, len(batch))


__all__ = ["default_batch", "load_batch", "save_batch"]
