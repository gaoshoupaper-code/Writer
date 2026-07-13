"""Spike 2：虚构历法验证。

假设：自定义 StoryCalendar 把"建元二十年冬"转成 datetime 作 reference_time，
使 Graphiti 的 valid_at 推算合理（"三年前结拜" → valid_at 早于当前约 3 年）。

做法：
  1. 定义最小 StoryCalendar（建元元年=2000-01-01，1 章节≈30 天）
  2. 第一段：reference_time=故事时间"建元十七年"（结拜时间点）
     第二段：reference_time=故事时间"建元二十年"（正文说"三年前结拜"）
  3. 取出边，检查 valid_at 是否落在合理区间（结拜早于当前约 3 年）

Pass 标准：valid_at 与 reference_time 的间隔合理（结拜边 valid_at ≈ 建元十七年）。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import build_graphiti
from graphiti_core_falkordb.nodes import EpisodeType


# ── 最小 StoryCalendar ──────────────────────────────────────────

# 建元元年 = 2000-01-01；1 建元年 = 365 天
JIAN_YUAN_EPOCH = datetime(2000, 1, 1)


def story_year_to_datetime(year: int, season: str = "") -> datetime:
    """建元 N 年 → datetime。season: 春/夏/秋/冬 调整月份。"""
    month = {"春": 3, "夏": 6, "秋": 9, "冬": 12}.get(season, 6)
    # 用近似日期：建元 N 年的 month 月 1 日
    return JIAN_YUAN_EPOCH.replace(year=JIAN_YUAN_EPOCH.year + year - 1, month=month)


# ── 测试语料 ────────────────────────────────────────────────────

EPISODE_BROTHERHOOD = """
建元十七年春，张三与李四在桃花峪折剑为誓，结为异姓兄弟，共誓同生共死。
"""

EPISODE_BETRAYAL = """
建元二十年冬，三年前张三与李四在桃花峪结拜。如今李四已投靠魔教，两人反目。
"""


async def main() -> None:
    print("═" * 60)
    print("Spike 2：虚构历法验证")
    print("═" * 60)

    graphiti, counter = build_graphiti()
    await graphiti.build_indices_and_constraints()

    # 第一段：结拜（建元十七年春 = 2000+16 年 = 2016-03-01）
    t_brotherhood = story_year_to_datetime(17, "春")
    print(f"▶ 灌入'结拜'段，reference_time = 建元十七年春 = {t_brotherhood.date()}")
    await graphiti.add_episode(
        name="calendar-brotherhood",
        episode_body=EPISODE_BROTHERHOOD,
        source_description="结拜时间点",
        reference_time=t_brotherhood,
        source=EpisodeType.text,
        group_id="spike2-calendar",
    )

    # 第二段：背叛（建元二十年冬 = 2000+19 年 = 2019-12-01），正文含"三年前结拜"
    t_betrayal = story_year_to_datetime(20, "冬")
    print(f"▶ 灌入'背叛'段，reference_time = 建元二十年冬 = {t_betrayal.date()}")
    await graphiti.add_episode(
        name="calendar-betrayal",
        episode_body=EPISODE_BETRAYAL,
        source_description="背叛时间点，含相对时间'三年前'",
        reference_time=t_betrayal,
        source=EpisodeType.text,
        group_id="spike2-calendar",
    )

    print(f"\n  LLM 调用 {counter.llm_calls} 次\n")

    # 取出所有边，检查 valid_at
    print("▶ 查询抽出的边及其 valid_at...")
    from graphiti_core_falkordb.edges import EntityEdge
    edges = await EntityEdge.get_by_group_ids(graphiti.driver, ["spike2-calendar"])
    # EntityEdge 有 valid_at / invalid_at

    print("─" * 60)
    print(f"{'关系边':<40} {'valid_at':<14} {'与背叛点间隔'}")
    print("─" * 60)

    brotherhood_edge_found = False
    for e in edges:
        if not hasattr(e, "valid_at") or e.valid_at is None:
            continue
        va = e.valid_at
        gap = t_betrayal - va if isinstance(va, datetime) else None
        gap_str = f"{gap.days}天" if gap else "?"
        # 尝试描述这条边
        fact = getattr(e, "fact", "") or getattr(e, "name", "") or "?"
        print(f"{str(fact)[:38]:<40} {str(va.date()):<14} {gap_str}")
        # 检查是否有"结拜"相关边且 valid_at 接近结拜时间（约 3 年前）
        if isinstance(va, datetime):
            delta = abs((va - t_brotherhood).days)
            if delta < 60:  # 2 个月内
                brotherhood_edge_found = True

    # verdict
    print("\n" + "═" * 60)
    print("VERDICT")
    print("═" * 60)
    expected_gap_days = (t_betrayal - t_brotherhood).days
    print(f"结拜时间点：{t_brotherhood.date()}")
    print(f"背叛时间点：{t_betrayal.date()}（间隔 {expected_gap_days} 天 ≈ 3 年）")
    print(f"'三年前结拜'应使结拜边 valid_at ≈ {t_brotherhood.date()}")
    print()
    if brotherhood_edge_found:
        print("✅ PASS —— 存在边的 valid_at 落在结拜时间点附近（±60 天）")
        print("   StoryCalendar + reference_time 方案可行")
    else:
        print("❌ FAIL —— 没有边的 valid_at 落在结拜时间点")
        print("   可能原因：①LLM 未正确推断相对时间 ②valid_at 取了 reference_time")
        print("   退路：改用章节号当时间锚点（ch0..chN 而非真实日期）")
    print()

    await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
