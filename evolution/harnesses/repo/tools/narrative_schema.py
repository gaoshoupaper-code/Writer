"""NWM 叙事学 Schema 配置（harness 可进化要素）。

去 Graphiti 重构（2026-07-17）：本文件不再是 Graphiti 的 entity_types/edge_types，
而是 NWM 记忆系统的"可进化 schema 策略"——影响抽取、检索、题材适配的行为。

与 executor 的分工（D-R5-3）：
  - executor 的 extractor.py 定义 ChapterRecords pydantic model（机器契约，数据结构骨架）。
  - executor 的 store.py 定义 _RECORD_TYPES（SQLite 表结构）。
  - 本文件（harness）定义 schema 的"可进化策略"：
      1. RECORD_TYPES_META：8 类 record 元信息（与 executor 对齐的人类可读契约）。
      2. GENRE_RECORD_POLICY：题材→启用 record 类型映射（D-D3-3）。
      3. FIELD_DESCRIPTION_OVERRIDES：字段中文描述（增强 extractor prompt 质量）。

evolution agent 可改本文件来：
  - 调整题材→record 映射（如玄幻启用 object_state，言情禁用）。
  - 优化字段描述（引导 LLM 抽取更准）。
  - 不改数据结构（那在 executor 的 pydantic model，是固定契约）。
"""
from __future__ import annotations

# ════════════════════════════════════════════════════════════════════
# 8 类 typed record 元信息（与 executor store._RECORD_TYPES 对齐）
# ════════════════════════════════════════════════════════════════════
# 这是人类可读契约：harness evolution agent 改字段描述时对照此表，
# 确保不偏离 executor 的实际表结构。
#
# 格式：record_type → (entity_col, (semantic_fields...), 中文用途说明)

RECORD_TYPES_META: dict[str, dict] = {
    "chapter_digest": {
        "entity_col": "source_chapter",
        "fields": ("summary", "key_events", "keyword_index"),
        "desc": "章节摘要（每章一条，事件/状态变化/场景骨架）",
    },
    "scene": {
        "entity_col": "scene_id",
        "fields": ("location", "participants", "event_order", "reveal_order", "summary"),
        "desc": "场景事件（location/participants/event_order故事序/reveal_order揭露序）",
    },
    "character_state": {
        "entity_col": "name",
        "fields": ("goal", "knowledge", "unknowns", "status", "location", "relationship_deltas"),
        "desc": "★ NWM 核心：角色状态（goal目标/knowledge知道什么/unknowns不知道什么——信息差追踪）",
    },
    "relationship_state": {
        "entity_col": "char_a",
        "fields": ("char_b", "relation_type", "polarity", "relationship_desc"),
        "desc": "关系状态（角色对/类型/极性正面负面/有效期）",
    },
    "object_state": {
        "entity_col": "name",
        "fields": ("owner", "location", "condition"),
        "desc": "关键物品状态（视题材启用，玄幻/悬疑常用，言情可禁用）",
    },
    "plot_promise": {
        "entity_col": "promise_id",
        "fields": ("thread_id", "structural_role", "status", "setup_chapter", "payoff_chapter", "promised_payoff", "resolution"),
        "desc": "★ 论文碾压点：伏笔/承诺（open/closed状态机，追踪挖坑填坑）",
    },
    "narrative_function": {
        "entity_col": "scene_ref",
        "fields": ("focalized_observer", "dramatic_beat", "turn_or_reversal", "reader_knowledge", "summary"),
        "desc": "★ 论文碾压点：叙事功能（focalized_observer视角/dramatic_beat拍子/reader_knowledge读者知晓——dramatic irony基础）",
    },
    "world_fact": {
        "entity_col": "fact",
        "fields": ("category", "scope", "valid_chapter_range"),
        "desc": "世界设定（势力/技术/魔法体系/社会结构）",
    },
}

# 全部 record 类型名（列表形式，便于遍历）
ALL_RECORD_TYPES: list[str] = list(RECORD_TYPES_META.keys())


# ════════════════════════════════════════════════════════════════════
# 题材→启用 record 类型映射（D-D3-3）
# ════════════════════════════════════════════════════════════════════
# 不同题材的创作重点不同，抽取时按题材跳过不相关的 record 类型，省 token。
# 未列出的题材或 default 用全启用（宁可多抽不漏）。
#
# evolution agent 可调整此映射来适配新题材或优化抽取成本。
# 值为"启用的 record 类型列表"，None 表示全启用。

GENRE_RECORD_POLICY: dict[str, list[str] | None] = {
    # 玄幻/仙侠：法宝、世界规则、伏笔都重要
    "玄幻": None,  # 全启用
    "仙侠": None,
    "奇幻": None,

    # 悬疑/推理：物品（证据）、叙事功能（视角/揭露）是核心
    "悬疑": None,
    "推理": None,

    # 言情/都市：物品状态通常不重要，可省 object_state
    "言情": ["chapter_digest", "scene", "character_state", "relationship_state",
              "plot_promise", "narrative_function", "world_fact"],
    "都市": ["chapter_digest", "scene", "character_state", "relationship_state",
             "plot_promise", "narrative_function", "world_fact"],

    # 历史/武侠：关系、伏笔、世界设定重要
    "历史": None,
    "武侠": None,

    # 科幻：世界设定、物品重要
    "科幻": None,

    # 默认：全启用（保险）
    "default": None,
}


def get_enabled_record_types(genre: str | None) -> list[str] | None:
    """按题材取启用的 record 类型列表（None=全启用）。

    被 ingestion 的 record_types_enabled 参数消费。
    题材未识别时走 default（全启用）。
    """
    if not genre:
        return GENRE_RECORD_POLICY.get("default")
    # 模糊匹配：题材字符串包含已知 key 即匹配（如"都市言情"→"都市"）
    for key, policy in GENRE_RECORD_POLICY.items():
        if key in genre:
            return policy
    return GENRE_RECORD_POLICY.get("default")


# ════════════════════════════════════════════════════════════════════
# 字段中文描述覆盖（增强 extractor prompt 质量）
# ════════════════════════════════════════════════════════════════════
# 这些描述可被 extractor 拼进 schema_hint，引导 LLM 更准抽取。
# evolution agent 可优化描述来改善抽取质量。
# 格式：(record_type, field_name) → 中文描述

FIELD_DESCRIPTION_OVERRIDES: dict[tuple[str, str], str] = {
    ("character_state", "knowledge"): "本章新增/确认的已知信息——NWM信息差追踪核心，只填本章明确建立的",
    ("character_state", "unknowns"): "本章确认角色尚不知道的信息——dramatic irony 关键",
    ("plot_promise", "promise_id"): "伏笔唯一标识，跨章节同名=同一伏笔（如'复仇之约'），保证状态机追踪",
    ("plot_promise", "status"): "open=本章新铺设/setup_chapter_hint填本章号；closed=本章兑现旧伏笔；updated=推进未兑现",
    ("narrative_function", "focalized_observer"): "视角人物：此场景通过谁的感知呈现（谁知，区别于谁说）",
    ("narrative_function", "reader_knowledge"): "读者此刻知道什么（vs角色知道什么，戏剧性反讽追踪）",
    ("scene", "reveal_order"): "揭露顺序：读者何时知道此事（可能与event_order不同，倒叙/回忆场景不同）",
}


# ── 向后兼容占位 ──
# 旧 Graphiti 版定义了 ENTITY_TYPES / EDGE_TYPES（给 add_episode 用）。
# NWM 重构后 executor 自带 schema，harness 不再需要这两个符号。
# 保留空 dict 导出仅为防止历史代码 import 报错（Phase 6 后无引用）。
ENTITY_TYPES: dict = {}
EDGE_TYPES: dict = {}


__all__ = [
    "RECORD_TYPES_META",
    "ALL_RECORD_TYPES",
    "GENRE_RECORD_POLICY",
    "get_enabled_record_types",
    "FIELD_DESCRIPTION_OVERRIDES",
    # 向后兼容
    "ENTITY_TYPES",
    "EDGE_TYPES",
]
