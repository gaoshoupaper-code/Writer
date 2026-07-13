"""Spike 1：中文抽取验证。

假设：Graphiti 能从中文小说正文抽取出纯中文实体名/关系（实体名 ≥90% 纯中文）。

做法：
  1. 灌入 CHAPTER_SAMPLE（~500 字中文玄幻）
  2. 取出所有实体节点，检查 name/summary 是否纯中文
  3. 打印实体列表 + 中文占比 + verdict

Pass 标准：实体名 ≥90% 纯中文，无中英混杂。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 让脚本能 import 上层 common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import build_graphiti, COUNTER, is_mostly_chinese
from graphiti_core_falkordb.nodes import EpisodeType
from fixtures import CHAPTER_SAMPLE


async def main() -> None:
    print("═" * 60)
    print("Spike 1：中文抽取验证")
    print("═" * 60)
    print(f"语料长度：{len(CHAPTER_SAMPLE)} 字\n")

    graphiti, counter = build_graphiti()

    # 建索引（首次必须）
    print("▶ 初始化图谱索引...")
    await graphiti.build_indices_and_constraints()
    print("  完成\n")

    # 灌入中文正文
    print("▶ add_episode（中文玄幻正文）...")
    result = await graphiti.add_episode(
        name="chinese-spike-1",
        episode_body=CHAPTER_SAMPLE,
        source_description="中文小说 spike 验证",
        reference_time=__import__("datetime").datetime(2025, 1, 1),
        source=EpisodeType.text,
        group_id="spike1-chinese",
    )
    print(f"  完成，LLM 调用 {counter.llm_calls} 次，token {counter.llm_total_tokens}\n")

    # 取出所有实体节点
    print("▶ 查询抽出的实体节点...")
    from graphiti_core_falkordb.nodes import EntityNode
    nodes = await EntityNode.get_by_group_ids(graphiti.driver, ["spike1-chinese"])

    entity_nodes = [n for n in nodes if hasattr(n, "summary")]
    print(f"  抽出 {len(entity_nodes)} 个实体节点\n")

    # 分析每个实体的中文纯度
    print("─" * 60)
    print(f"{'实体名':<16} {'类型':<10} {'name中文?':<10} {'summary中文?':<12}")
    print("─" * 60)

    name_ok = 0
    summary_ok = 0
    for n in entity_nodes:
        labels = ",".join(n.labels) if n.labels else "-"
        name_zh, name_ratio = is_mostly_chinese(n.name or "", threshold=0.5)
        sum_zh, sum_ratio = is_mostly_chinese(n.summary or "", threshold=0.5)
        if name_zh:
            name_ok += 1
        if sum_zh:
            summary_ok += 1
        print(f"{(n.name or '')[:14]:<16} {labels:<10} "
              f"{'✓' if name_zh else '✗':<10} {'✓' if sum_zh else '✗':<12}")
        if n.summary:
            print(f"  summary: {n.summary[:80]}")
        print()

    total = len(entity_nodes) or 1
    name_pct = name_ok / total
    summary_pct = summary_ok / total

    # verdict
    print("═" * 60)
    print("VERDICT")
    print("═" * 60)
    passed = name_pct >= 0.9
    print(f"实体名中文达标率：{name_ok}/{total} = {name_pct:.0%}（阈值 90%）")
    print(f"summary 中文达标率：{summary_ok}/{total} = {summary_pct:.0%}")
    print(f"LLM 调用：{counter.llm_calls} 次，token：{counter.llm_total_tokens}")
    print()
    if passed:
        print("✅ PASS —— Graphiti 默认能从中文抽出纯中文实体名")
        print("   （若仍有混杂，需覆写 prompts/ 加中文约束）")
    else:
        print("❌ FAIL —— 实体名中英混杂超过 10%")
        print("   必须覆写 graphiti_core_falkordb/prompts/ 加中文约束后再测")
    print()

    await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
