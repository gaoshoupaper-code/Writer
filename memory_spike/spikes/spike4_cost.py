"""Spike 4：成本 dry-run。

假设：单章抽取的 LLM 调用次数 + token 量在可接受范围。

做法：
  1. 用真实 1 章（~1000 字，取自 fixtures.CHAPTER_SAMPLE 或扩展）
  2. 跑完整 add_episode
  3. 埋点记录 LLM 调用次数 + token 总量
  4. 算单章成本 + 外推 50 章成本

Pass 标准：单章 ≤15 次 LLM 调用，token ≤20k。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import build_graphiti, COUNTER
from graphiti_core_falkordb.nodes import EpisodeType
from fixtures import CHAPTER_SAMPLE


# 模拟 1 章正文（~1000 字，基于 CHAPTER_SAMPLE 扩展到接近真实章节长度）
CHAPTER_FULL = CHAPTER_SAMPLE + """

张三将信纸收入怀中，对苏瑶道："走，即刻下山。"两人连夜离开桃花峪，取道北行。
一路上风雪交加，张三心中反复回想着与李四过往的种种。当年两人同在桃花峪学艺，
李四性格温和，张三性子刚烈，一柔一刚，相得益彰。师父常说，他们是天造地设的
一对师兄弟。

行至洛阳城外，张三忽然停下脚步。前方官道上，一队黑衣人正押解着几辆囚车缓缓
前行。囚车里关押的，竟都是正道各派弟子。

"魔教的人。"苏瑶低声道，"看旗帜，是血煞堂。"

张三握紧玄铁剑。他本不想多管闲事，但见囚车中一名少女胸前绣着青云派的云纹，
那是他故人之女。当年青云派掌门对他有半师之谊，临终前托他照拂门下。

"救人。"张三只说了两个字，便如离弦之箭般冲了出去。玄铁剑出鞘，寒光凛冽，
剑气纵横间，血煞堂众黑衣人纷纷倒地。苏瑶紧随其后，天音诀化作无形音刃，将
残余黑衣人的兵刃尽数震落。

救出众人后，那青云派少女泣道："张大侠，家师……家师已被魔教杀害。魔教教主
扬言，三月之内要荡平正道六派。"

张三心中一沉。魔教教主，正是李四的师父。此番李四约他相见，恐怕正是为此。
"""


async def main() -> None:
    print("═" * 60)
    print("Spike 4：成本 dry-run")
    print("═" * 60)
    print(f"单章正文长度：{len(CHAPTER_FULL)} 字\n")

    graphiti, counter = build_graphiti()
    counter.reset()  # 确保干净统计
    await graphiti.build_indices_and_constraints()

    print("▶ 跑完整 add_episode（埋点统计中）...\n")
    result = await graphiti.add_episode(
        name="cost-dryrun",
        episode_body=CHAPTER_FULL,
        source_description="成本 dry-run 单章",
        reference_time=__import__("datetime").datetime(2025, 1, 1),
        source=EpisodeType.text,
        group_id="spike4-cost",
    )

    s = counter.summary()
    print("─" * 60)
    print("单章成本统计")
    print("─" * 60)
    print(f"LLM 调用次数：    {s['llm_calls']}")
    print(f"Embedding 调用：   {s['embed_calls']}")
    print(f"总调用次数：       {s['total_calls']}")
    print(f"LLM prompt tokens：{s['llm_prompt_tokens']:,}")
    print(f"LLM comp tokens：  {s['llm_completion_tokens']:,}")
    print(f"LLM 总 tokens：    {s['llm_total_tokens']:,}")
    print()

    # 逐次调用明细
    print("─" * 60)
    print("逐次调用明细")
    print("─" * 60)
    for i, ev in enumerate(counter.events, 1):
        if ev["type"] == "llm":
            print(f"{i:>3}. LLM [{ev['label'][:30]:<30}] "
                  f"prompt={ev['prompt_tokens']:>6} comp={ev['completion_tokens']:>5} "
                  f"total={ev['prompt_tokens']+ev['completion_tokens']:>6}")
        else:
            print(f"{i:>3}. EMBED")
    print()

    # 成本外推
    model = os.environ.get("SPIKE_LLM_MODEL", "gpt-4o-mini")
    # gpt-4o-mini: input $0.15/1M, output $0.60/1M（截至 2025）
    # 用近似单价
    prices = {
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
        "deepseek-chat": (0.14, 0.28),
    }
    in_price, out_price = prices.get(model, (1.0, 2.0))
    single_cost = (
        s["llm_prompt_tokens"] / 1_000_000 * in_price
        + s["llm_completion_tokens"] / 1_000_000 * out_price
    )
    print("─" * 60)
    print("成本外推")
    print("─" * 60)
    print(f"模型：{model}（input ${in_price}/1M, output ${out_price}/1M）")
    print(f"单章抽取成本：${single_cost:.4f}")
    print(f"50 章小说：  ${single_cost*50:.3f}")
    print(f"100 章小说： ${single_cost*100:.3f}")
    print()

    # verdict
    print("═" * 60)
    print("VERDICT")
    print("═" * 60)
    call_ok = s["llm_calls"] <= 15
    token_ok = s["llm_total_tokens"] <= 20_000
    print(f"LLM 调用 {s['llm_calls']} 次（阈值 ≤15）：{'✅' if call_ok else '❌'}")
    print(f"Token {s['llm_total_tokens']:,}（阈值 ≤20k）：{'✅' if token_ok else '❌'}")
    print()
    if call_ok and token_ok:
        print("✅ PASS —— 单章成本在可接受范围")
    else:
        print("❌ FAIL —— 成本超预期")
        print("   缓解：①分层模型 small_model 跑 dedup ②combined extraction")
        print("        ③评估是否回退路径 C（sqlite-vec，零 LLM 抽取）")
    print()

    await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
