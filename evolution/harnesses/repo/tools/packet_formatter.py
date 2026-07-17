"""证据包排版器（harness 可进化要素）。

把检索命中的 anchors + expanded records 排版成注入 writing prompt 的文本。
排版质量直接影响 writing agent 能否用好召回的记忆。

evolution agent 可改本文件来调整排版策略（如调叙事优先级、加减 evidence 显示）。
改完 assemble 注入后立即生效。

签名约束（executor retriever 调用契约）：
  packet_formatter(anchors, expanded, chapter_num) -> str
  anchors/expanded 的每行 dict 含 _record_type + 原始 record 字段。

与 executor 默认实现的关系：executor retriever 内置 default_packet_formatter。
harness 提供本文件时，assemble 注入覆盖默认。
"""
from __future__ import annotations

import json
from typing import Any


def packet_formatter(
    anchors: list[dict],
    expanded: list[dict],
    chapter_num: int | None,
) -> str:
    """证据包排版（按叙事优先级分组 + evidence 溯源）。

    优先级：角色→关系→场景→伏笔→叙事功能→物品→设定→章节摘要。
    角色放最前（写作时最常用），伏笔/叙事功能靠前（NWM 差异化价值）。
    """
    if not anchors and not expanded:
        return ""

    all_records = anchors + expanded
    by_type: dict[str, list[dict]] = {}
    for r in all_records:
        by_type.setdefault(r["_record_type"], []).append(r)

    priority = [
        ("character_state", "【角色状态】"),
        ("relationship_state", "【人物关系】"),
        ("scene", "【场景事件】"),
        ("plot_promise", "【伏笔追踪】"),
        ("narrative_function", "【叙事功能】"),
        ("object_state", "【关键物品】"),
        ("world_fact", "【世界设定】"),
        ("chapter_digest", "【章节摘要】"),
    ]

    sections: list[str] = []
    for rt, label in priority:
        records = by_type.get(rt, [])
        if not records:
            continue
        seen: set[int] = set()
        lines: list[str] = []
        for r in records:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            line = _format_record_line(rt, r)
            if line:
                lines.append(line)
        if lines:
            sections.append(f"{label}\n" + "\n".join(lines))

    return "\n\n".join(sections)


def _format_record_line(record_type: str, row: dict) -> str:
    """单条 record 排版（带章节溯源 + evidence 引用 + 关联标记）。"""
    ch = row.get("source_chapter", "?")
    via = "（关联）" if row.get("_via_join") else ""
    ev = row.get("evidence_span", "")

    if record_type == "character_state":
        name = row.get("name", "")
        parts = [f"- {name}"]
        if row.get("goal"): parts.append(f"目标：{row['goal']}")
        if row.get("status"): parts.append(f"状态：{row['status']}")
        if row.get("location"): parts.append(f"位置：{row['location']}")
        if row.get("knowledge"):
            try:
                k = json.loads(row["knowledge"]) if isinstance(row["knowledge"], str) else row["knowledge"]
                if k: parts.append(f"知道：{'、'.join(k)}")
            except Exception:
                pass
        parts.append(f"（第{ch}章确立）{via}")
        if ev: parts.append(f"｜原文：{ev[:60]}")
        return " ".join(parts)

    if record_type == "relationship_state":
        return (
            f"- {row.get('char_a','')} 与 {row.get('char_b','')}："
            f"{row.get('relationship_desc','')}（{row.get('polarity','')}）（第{ch}章）{via}"
        )

    if record_type == "plot_promise":
        status = row.get("status", "")
        payoff = row.get("payoff_chapter")
        status_text = f"已兑现@第{payoff}章" if status == "closed" and payoff else "未兑现"
        return f"- 「{row.get('promise_id','')}」{status_text}：{row.get('promised_payoff','')}（第{ch}章铺设）{via}"

    if record_type == "scene":
        return f"- 场景@{row.get('location','')}：{row.get('summary','')}（第{ch}章）{via}"

    if record_type == "narrative_function":
        obs = row.get("focalized_observer", "")
        beat = row.get("dramatic_beat", "")
        return f"- 视角{obs}（{beat}）：{row.get('summary','')}（第{ch}章）{via}"

    if record_type == "object_state":
        return f"- {row.get('name','')}：{row.get('owner','')}持有，{row.get('condition','')}（第{ch}章）{via}"

    if record_type == "world_fact":
        return f"- {row.get('fact','')}（{row.get('category','')}）（第{ch}章）{via}"

    if record_type == "chapter_digest":
        return f"- 第{ch}章：{row.get('summary','')}"

    return f"- [{record_type}]（第{ch}章）{via}"


__all__ = ["packet_formatter"]
