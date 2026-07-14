"""叙事学类型化 Schema（可进化要素）。

定义 Graphiti add_episode 的 entity_types / edge_types，
引导 LLM 抽取叙事专用实体/关系，而非 Graphiti 默认的通用实体。

设计依据：
  - 设计方案 §5（NWM 叙事学类型化 schema）
  - 设计文档 §2.1（narrative_schema 落地形式）

约束（Graphiti validate_entity_types）：
  自定义类型的字段名不能与 EntityNode 冲突——
  保留名：uuid / name / group_id / labels / created_at / summary / name_embedding。
  所以每个实体的自定义字段都避开这些保留名。

P1 范围：Character / Location / WorldFact / Thread / StoryNode（5 实体类型）。
Promise / 伏笔状态机留 P2。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── 实体类型 ────────────────────────────────────────────────────────

class CharacterEntity(BaseModel):
    """角色节点。对应 workspace/character/*.md。"""
    aliases: list[str] = Field(
        default=[], description="角色的别名/称呼列表，如 ['张大侠', '张公子']"
    )
    role_type: str = Field(
        default="", description="角色类型：主角/配角/反派/核心人物等"
    )
    arc_summary: str = Field(
        default="", description="角色弧光简述：从什么状态到什么状态"
    )


class LocationEntity(BaseModel):
    """地点节点。对应 worldview.md 中的地点 + storyline 中的场景。"""
    region: str = Field(default="", description="所属区域/势力范围")
    description_short: str = Field(default="", description="地点简述")


class WorldFactEntity(BaseModel):
    """世界规则/设定节点。对应 worldview.md 中的核心规则。"""
    category: str = Field(default="", description="规则类别：势力/技术/魔法体系/社会结构等")
    rule_text: str = Field(default="", description="规则简述")


class ThreadEntity(BaseModel):
    """故事线节点。对应 storyline/S0X-*.md。"""
    thread_type: str = Field(default="", description="主线/支线")
    status: str = Field(default="活跃", description="活跃/完结/暂停")


class StoryNodeEntity(BaseModel):
    """叙事事件节点。对应 storyline E0XX 事件。

    这是 NWM 的核心——把叙事分解为可查询的事件序列，
    每个事件带叙事类型、事件组（叙事节奏）、故事内时间。
    """
    event_type: str = Field(
        default="",
        description="叙事类型：冲突/危机/反转/悬念/揭露/胜利/交汇"
    )
    event_group: str = Field(
        default="",
        description="叙事节奏组：G00开端/G01发展/G02终局"
    )
    story_time: str = Field(
        default="",
        description="故事内时间，如 '2087年2月15日' 或 '建元二十年冬'"
    )


# ── 关系类型 ────────────────────────────────────────────────────────
# Graphiti 的 edge 已内置 valid_at / invalid_at（双时间戳），
# 这里只定义额外的叙事学语义字段（不重复时间字段）。

class RelationshipEdge(BaseModel):
    """角色间关系（带极性）。valid_at/invalid_at 由 Graphiti 管理。"""
    polarity: str = Field(
        default="中性",
        description="关系极性：正面/负面/中性/矛盾"
    )
    relationship_desc: str = Field(
        default="", description="关系简述，如 '师徒' '宿敌' '盟友'"
    )


class ParticipatesInEdge(BaseModel):
    """角色参与某事件（StoryNode）。"""
    participation_role: str = Field(
        default="",
        description="参与角色：主角/目击者/推动者/受害者"
    )


class BelongsToThreadEdge(BaseModel):
    """事件/角色属于某故事线（Thread）。"""
    pass  # 纯类型边，无额外字段


class LocatedAtEdge(BaseModel):
    """角色/事件位于某地点。"""
    pass


class CausedByEdge(BaseModel):
    """事件因果（A 由 B 引起）。支持多跳因果推理。"""
    pass


# ── Schema 聚合（供 add_episode 调用）──────────────────────────────

ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Character": CharacterEntity,
    "Location": LocationEntity,
    "WorldFact": WorldFactEntity,
    "Thread": ThreadEntity,
    "StoryNode": StoryNodeEntity,
}

EDGE_TYPES: dict[str, type[BaseModel]] = {
    "RELATIONSHIP": RelationshipEdge,
    "PARTICIPATES_IN": ParticipatesInEdge,
    "BELONGS_TO_THREAD": BelongsToThreadEdge,
    "LOCATED_AT": LocatedAtEdge,
    "CAUSED_BY": CausedByEdge,
}


__all__ = [
    "CharacterEntity",
    "LocationEntity",
    "WorldFactEntity",
    "ThreadEntity",
    "StoryNodeEntity",
    "RelationshipEdge",
    "ParticipatesInEdge",
    "BelongsToThreadEdge",
    "LocatedAtEdge",
    "CausedByEdge",
    "ENTITY_TYPES",
    "EDGE_TYPES",
]
