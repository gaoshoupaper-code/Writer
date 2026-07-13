"""Spike 3：别名消歧验证。

假设：连续 add_episode 能把"张三/张大侠/张公子"合并成 1 个节点（Issue #963 风险）。

做法：
  1. 连续灌 3 段文本，分别含"张三""张大侠""张公子"
  2. 灌 2 段对照（李四/苏瑶，明确不同人）
  3. 查图谱：名为"张三"的节点有几个？所有实体节点列出来
  4. 判断是否误合并（李四/苏瑶不该并入张三）

Pass 标准：张三相关称呼合并成 ≤2 节点（理想 1 个，允许 2 个）；李四/苏瑶独立。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import build_graphiti
from graphiti_core_falkordb.nodes import EpisodeType
from fixtures import ALIAS_FRAGMENTS, DISTINCT_FRAGMENTS


async def main() -> None:
    print("═" * 60)
    print("Spike 3：别名消歧验证")
    print("═" * 60)

    graphiti, counter = build_graphiti()
    await graphiti.build_indices_and_constraints()

    import datetime

    # 灌入 3 段张三别名
    print("▶ 灌入 3 段张三不同称呼...")
    for i, frag in enumerate(ALIAS_FRAGMENTS):
        await graphiti.add_episode(
            name=frag["name"],
            episode_body=frag["text"],
            source_description="别名消歧测试",
            reference_time=datetime.datetime(2025, 1, 1) + datetime.timedelta(days=i),
            source=EpisodeType.text,
            group_id="spike3-alias",
        )
        print(f"  ✓ {frag['name']}: {frag['text'][:20]}...")

    # 灌入对照（李四/苏瑶）
    print("\n▶ 灌入 2 段对照（李四/苏瑶）...")
    for i, frag in enumerate(DISTINCT_FRAGMENTS):
        await graphiti.add_episode(
            name=frag["name"],
            episode_body=frag["text"],
            source_description="别名消歧对照",
            reference_time=datetime.datetime(2025, 1, 10) + datetime.timedelta(days=i),
            source=EpisodeType.text,
            group_id="spike3-alias",
        )
        print(f"  ✓ {frag['name']}: {frag['text'][:20]}...")

    print(f"\n  共 {counter.llm_calls} 次 LLM 调用\n")

    # 查所有节点
    print("▶ 查询所有实体节点...")
    nodes = await graphiti.get_nodes_by_group(group_id="spike3-alias")
    entity_nodes = [n for n in nodes if hasattr(n, "summary")]

    print("─" * 60)
    print(f"{'实体名':<16} {'labels':<16} {'summary 前 40'}")
    print("─" * 60)
    for n in entity_nodes:
        labels = ",".join(n.labels) if n.labels else "-"
        print(f"{(n.name or '')[:14]:<16} {labels:<16} {(n.summary or '')[:40]}")
    print()

    # 分析：张三相关节点数
    zhang_nodes = [n for n in entity_nodes if n.name and (
        "张三" in n.name or "张大侠" in n.name or "张公子" in n.name or n.name == "张")]
    li_nodes = [n for n in entity_nodes if n.name and "李四" in n.name]
    su_nodes = [n for n in entity_nodes if n.name and "苏瑶" in n.name]

    # verdict
    print("═" * 60)
    print("VERDICT")
    print("═" * 60)
    print(f"张三系（张三/张大侠/张公子）节点数：{len(zhang_nodes)}（理想 1，允许 2）")
    print(f"李四节点数：{len(li_nodes)}（期望 1）")
    print(f"苏瑶节点数：{len(su_nodes)}（期望 1）")
    print()

    alias_ok = len(zhang_nodes) <= 2
    distinct_ok = len(li_nodes) >= 1 and len(su_nodes) >= 1

    if alias_ok and distinct_ok:
        print("✅ PASS —— 别名合并可接受 + 不同角色未误合并")
        if len(zhang_nodes) == 2:
            print("   ⚠️ 合并成 2 节点（非理想 1），但可接受")
            print("   生产建议：加规范名表预处理（别名→主名替换）降到 1")
    elif alias_ok and not distinct_ok:
        print("❌ FAIL —— 李四/苏瑶被误合并或缺失")
    else:
        print("❌ FAIL —— 张三系产生 >2 个重复节点")
        print("   必须上规范名表预处理层（storybuilding 产别名映射，抽取前替换）")
    print()

    await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
