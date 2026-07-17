"""查询构造器（harness 可进化要素）。

把 writing subagent 的 task description 转成 writer query（供检索用）。
这是检索质量的关键杠杆——查询构造得好，召回才准。

evolution agent 可改本文件来优化查询构造策略（如加实体提取、查询改写）。
改完 assemble 注入后立即生效。

与 executor 的关系：executor 的 MemoryRecallMiddleware 默认内联构造查询，
assemble 时若 harness 提供了 query_builder，middleware 用 harness 版本。
"""
from __future__ import annotations

import re

# 章节号提取正则（与 middleware 保持一致，harness 可独立改）
_CHAPTER_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*章"),
    re.compile(r"第\s*([一二三四五六七八九十百]+)\s*章"),
    re.compile(r"chapter[\s\-]*(\d+)", re.IGNORECASE),
]
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def extract_chapter_index(text: str) -> int | None:
    """从文本提取章节号（支持第3章/第三章/chapter-03）。"""
    if not text:
        return None
    for pattern in _CHAPTER_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            if raw.isdigit():
                return int(raw)
            return _cn_to_int(raw)
    return None


def _cn_to_int(cn: str) -> int | None:
    total = 0
    section = 0
    for ch in cn:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            total += (section if section else 1) * 10
            section = 0
    total += section
    return total if total > 0 else None


def build_query(task: str, chapter_num: int | None) -> str:
    """从 task description 构造 writer query（harness 可进化）。

    策略：task 通常含"写第N章"+"本章事件E0XX"+"出场角色"等。
    提取关键实体（角色名、事件描述）作为查询，截断防超长。

    evolution 可优化：加 NLP 实体识别、查询改写、多查询融合等。
    """
    if not task:
        return ""
    # P1 策略：直接用 task 原文截断（FTS5 BM25/LIKE 匹配关键词）。
    # TODO（evolution 可改进）：提取角色名/事件ID 作为查询焦点，去掉系统指令噪声。
    return task[:2000]


__all__ = ["build_query", "extract_chapter_index"]
