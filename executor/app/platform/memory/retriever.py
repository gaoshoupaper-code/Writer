"""NWM 四阶段检索编排（论文 §A.1）。

Retrieve(M_n, q) 四阶段：
  1. Causal Restriction（因果截止）：只搜 source_chapter <= cutoff 的记录（杜绝未来章节泄漏）。
  2. Hybrid Node Ranking（混合排序）：FTS5（BM25）+ sqlite-vec（向量）两路，RRF 融合。
  3. One-Hop Graph Expansion（一跳扩展）：anchor 节点的邻域 JOIN（关系/场景/物品/伏笔）。
  4. Bounded Packet Assembly（有界组装）：字符预算截断 + evidence 引用 → EvidencePacket。

为什么不用 Graphiti search_：
  Graphiti 的 generic entity/edge 检索缺叙事学类型（论文 §6 批评点）。
  本模块用 store 的强类型查询原语自编排四阶段，每个阶段的命中可解释、可审计。

设计依据：设计文档 D-D4-1（四阶段）、D-D4-2（JOIN 规则 harness 可进化）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from app.platform.memory.store import MemoryStore, _RECORD_TYPES

logger = logging.getLogger(__name__)

# RRF 常数（论文 Cormack et al. 2009 标准 k=61，业内常用 60）
_RRF_K = 60


# ════════════════════════════════════════════════════════════════════
# 默认 one-hop JOIN 规则（harness 可覆盖，Phase 5 抽到 join_rules.py）
# ════════════════════════════════════════════════════════════════════

def default_join_rules(
    store: MemoryStore,
    anchor_type: str,
    anchor_row: dict,
    cutoff: int,
) -> list[dict]:
    """从 anchor 节点扩展一跳邻域（论文阶段3）。

    论文 §A.1：one-hop 把"排名的节点集"转成"连通的类型化结构"——
    暴露 knowledge/location/relationship-delta/reveal/thread-resolution 边。

    anchor 类型决定扩展哪些 record（harness 可进化覆盖此函数）：
      - character_state → 该角色参与的 scene + relationship_state + object_state + plot_promise
      - scene → 参与该场景的 character_state
      - plot_promise → 关联 thread 的 character_state（简略）
      - 其他 → 不扩展（叶子）

    Returns: 扩展出的 record 行列表（去重，每行带 _via 字段标记来源 anchor）。
    """
    expanded: list[dict] = []
    seen_ids: set[tuple[str, int]] = set()  # (record_type, id) 去重

    def _add(rt: str, rows: list[dict]) -> None:
        for r in rows:
            key = (rt, r["id"])
            if key not in seen_ids:
                r["_record_type"] = rt
                r["_via_join"] = True  # 标记是 JOIN 扩展来的（非直接命中）
                expanded.append(r)
                seen_ids.add(key)

    if anchor_type == "character_state":
        name = anchor_row.get("name", "")
        if name:
            # 该角色参与的场景（通过 participants JSON 包含角色名匹配）
            # SQLite JSON 查询 participants（存为 JSON 字符串）
            scenes = store.conn.execute(
                "SELECT * FROM scene WHERE source_chapter <= ? "
                "AND (participants LIKE ? OR summary LIKE ?)",
                [cutoff, f'%"{name}"%', f"%{name}%"],
            ).fetchall()
            _add("scene", [dict(r) for r in scenes])

            # 该角色的关系（char_a 或 char_b 匹配）
            rels = store.conn.execute(
                "SELECT * FROM relationship_state WHERE source_chapter <= ? "
                "AND (char_a = ? OR char_b = ?)",
                [cutoff, name, name],
            ).fetchall()
            _add("relationship_state", [dict(r) for r in rels])

            # 该角色持有的物品
            objs = store.conn.execute(
                "SELECT * FROM object_state WHERE source_chapter <= ? AND owner = ?",
                [cutoff, name],
            ).fetchall()
            _add("object_state", [dict(r) for r in objs])

    elif anchor_type == "scene":
        # 参与该场景的角色：取场景记录的 participants（JSON 数组）
        participants_raw = anchor_row.get("participants", "[]")
        try:
            import json
            names = json.loads(participants_raw) if isinstance(participants_raw, str) else participants_raw
        except Exception:
            names = []
        for nm in names:
            if nm:
                chars = store.get_current_state("character_state", cutoff=cutoff, entity=nm, limit=1)
                _add("character_state", chars)

    elif anchor_type == "plot_promise":
        # 伏笔关联的角色：通过 thread_id 间接关联（简化：不深挖，伏笔本身信息已足够）
        pass

    return expanded


# ════════════════════════════════════════════════════════════════════
# EvidencePacket（检索结果，供 trace 审计 + middleware 注入）
# ════════════════════════════════════════════════════════════════════

class EvidencePacket:
    """检索返回的有界证据包（论文阶段4 bounded packet）。

    formatted：排版好的文本（注入 writing prompt）。
    其他字段供 trace 审计（D-R1-3 完整检索审计）。
    """

    def __init__(self) -> None:
        self.nodes: list[dict] = []          # 直接命中的 record（阶段2）
        self.edges: list[dict] = []          # JOIN 扩展的 record（阶段3）
        self.formatted: str = ""             # 排版文本（≤ budget_chars）
        self.token_estimate: int = 0         # 估算 token（中文 ~1.5字/token）
        self.query_used: str = ""            # 实际查询串
        # trace 审计字段（D-D4-2）
        self.hits: list[dict] = []           # 命中摘要（type/name/source_chapter/valid）
        self.causal_cutoff: int | None = None
        self.stage1_count: int = 0           # cutoff 后总记录数
        self.stage2_anchors: list[str] = []  # RRF 融合后的 anchor 标识
        self.stage3_expanded: int = 0        # JOIN 扩展出多少条
        self.truncated: bool = False         # 是否被 budget 截断

    def __bool__(self) -> bool:
        """空证据包（无内容）判 False，middleware 据此决定是否注入。"""
        return bool(self.formatted.strip())


# ════════════════════════════════════════════════════════════════════
# MemoryRetriever — 四阶段检索编排
# ════════════════════════════════════════════════════════════════════

class MemoryRetriever:
    """四阶段检索编排器。组合 store 原语执行论文 §A.1 流程。

    生命周期：进程单例（无状态，操作时传入 store）。
    join_rules：harness 可覆盖（D-D4-2），None 用 default_join_rules。
    """

    def __init__(
        self,
        join_rules: Callable[..., list[dict]] | None = None,
        packet_formatter: Callable[[list[dict], list[dict], int | None], str] | None = None,
    ) -> None:
        self._join_rules = join_rules or default_join_rules
        self._packet_formatter = packet_formatter or default_packet_formatter

    async def retrieve(
        self,
        store: MemoryStore,
        query: str,
        *,
        group_id: str,  # 兼容旧接口（MemoryBackend.retrieve 签名），实际不用（一库一作品）
        query_embedding: list[float] | None = None,
        causal_cutoff: int | None = None,
        budget_chars: int = 12000,
        num_results: int = 10,
    ) -> EvidencePacket:
        """执行四阶段检索。

        Args:
            store: 该作品的 MemoryStore。
            query: writer query（harness query_builder 构造）。
            group_id: 作品标识（兼容旧接口，一库一作品下不用）。
            query_embedding: query 的向量（None 时跳过向量路，只用 BM25/LIKE）。
            causal_cutoff: 因果截止章节号（None 不截止——不推荐，违反论文）。
            budget_chars: 证据包字符预算。
            num_results: 阶段2 每路返回上限。

        Returns:
            EvidencePacket（含 formatted + trace 审计字段）。
        """
        packet = EvidencePacket()
        packet.query_used = query
        packet.causal_cutoff = causal_cutoff

        if not query.strip():
            return packet

        # ── 阶段1：Causal Restriction ──
        # cutoff=None 时用一个足够大的值（检索全部，但生产环境必传 cutoff）
        cutoff = causal_cutoff if causal_cutoff is not None else 999999
        packet.stage1_count = sum(
            store.count_records(rt, cutoff=cutoff) for rt in _RECORD_TYPES
        )
        if packet.stage1_count == 0:
            logger.debug("记忆库为空（cutoff=%s），返回空包", cutoff)
            return packet

        # ── 阶段2：Hybrid Node Ranking（FTS5 + vec，RRF 融合）──
        anchors = self._hybrid_rank(store, query, query_embedding, cutoff, num_results)
        packet.stage2_anchors = [
            f"{a['_record_type']}:{a.get(_entity_col_of(a['_record_type']), a.get('id'))}"
            for a in anchors
        ]

        if not anchors:
            logger.debug("阶段2 无命中（query=%s... cutoff=%s）", query[:30], cutoff)
            return packet

        # ── 阶段3：One-Hop Graph Expansion ──
        expanded: list[dict] = []
        for anchor in anchors:
            ex = self._join_rules(store, anchor["_record_type"], anchor, cutoff)
            expanded.extend(ex)
        packet.stage3_expanded = len(expanded)

        # ── 阶段4：Bounded Packet Assembly ──
        # 合并 anchors + expanded，去重，按 budget 截断
        all_records = self._merge_dedupe(anchors, expanded)
        packet.nodes = [{"type": r["_record_type"], **_summarize(r)} for r in anchors]
        packet.edges = [{"type": r["_record_type"], **_summarize(r)} for r in expanded]
        packet.hits = [
            {
                "type": r["_record_type"],
                "name": r.get(_entity_col_of(r["_record_type"]), ""),
                "source_chapter": r.get("source_chapter"),
                "via_join": r.get("_via_join", False),
            }
            for r in all_records
        ]

        packet.formatted = self._packet_formatter(anchors, expanded, causal_cutoff)
        if len(packet.formatted) > budget_chars:
            packet.formatted = packet.formatted[:budget_chars]
            packet.truncated = True
        packet.token_estimate = int(len(packet.formatted) * 1.5)

        logger.debug(
            "检索完成：query=%s... cutoff=%s stage1=%d anchors=%d expanded=%d chars=%d",
            query[:30], cutoff, packet.stage1_count,
            len(anchors), len(expanded), len(packet.formatted),
        )
        return packet

    def _hybrid_rank(
        self,
        store: MemoryStore,
        query: str,
        query_embedding: list[float] | None,
        cutoff: int,
        num_results: int,
    ) -> list[dict]:
        """阶段2：FTS5 + 向量两路检索，RRF 融合。

        FTS5 路：统一 records_fts 表（跨所有 record 类型），返回 record_type + rowid_of_record。
        向量路：按 record_type 分别查各 vec 表（向量按类型分表存）。
        RRF：score = Σ 1/(k + rank)，融合后取 top-N。
        """
        # FTS5 路（BM25/LIKE，跨类型）
        fts_hits = store.fts_search(query, cutoff=cutoff, limit=num_results * 2)
        # rowid_of_record → (record_type, rank)
        fts_rank: dict[tuple[str, int], int] = {}
        for i, h in enumerate(fts_hits):
            key = (h["record_type"], h["rowid_of_record"])
            if key not in fts_rank:  # 取首次出现（FTS 已按 rank 排序）
                fts_rank[key] = i + 1  # rank 从 1 开始

        # 向量路（按 record_type 分表查）
        vec_rank: dict[tuple[str, int], int] = {}
        if query_embedding is not None:
            vec_idx = 0
            per_type = max(1, num_results // len(_RECORD_TYPES))  # 每类均分配额
            for rt in _RECORD_TYPES:
                try:
                    hits = store.vec_search(rt, query_embedding, cutoff=cutoff, limit=per_type)
                except Exception:
                    continue
                for h in hits:
                    key = (rt, h["id"])
                    if key not in vec_rank:
                        vec_idx += 1
                        vec_rank[key] = vec_idx

        # RRF 融合
        all_keys = set(fts_rank) | set(vec_rank)
        scored: list[tuple[float, tuple[str, int]]] = []
        for key in all_keys:
            score = 0.0
            if key in fts_rank:
                score += 1.0 / (_RRF_K + fts_rank[key])
            if key in vec_rank:
                score += 1.0 / (_RRF_K + vec_rank[key])
            scored.append((score, key))
        scored.sort(key=lambda x: -x[0])  # 降序

        # 取 top-N，回填完整 record 行
        top_keys = [k for _, k in scored[:num_results]]
        anchors: list[dict] = []
        for rt, rid in top_keys:
            rows = store.fetch_records_by_ids(rt, [rid])
            if rows:
                row = rows[0]
                row["_record_type"] = rt
                row["_via_join"] = False
                anchors.append(row)
        return anchors

    def _merge_dedupe(self, anchors: list[dict], expanded: list[dict]) -> list[dict]:
        """合并 anchors + expanded，按 (type, id) 去重，anchors 优先。"""
        seen: set[tuple[str, int]] = set()
        merged: list[dict] = []
        for r in anchors + expanded:
            key = (r["_record_type"], r["id"])
            if key not in seen:
                seen.add(key)
                merged.append(r)
        return merged


# ── 辅助 ────────────────────────────────────────────────────────────

def _entity_col_of(record_type: str) -> str:
    """取 record 类型的实体列名（用于 hits 摘要）。"""
    return _RECORD_TYPES[record_type][0] if record_type in _RECORD_TYPES else "id"


def _summarize(row: dict) -> dict:
    """从 record 行提取摘要字段（供 packet.nodes/edges）。"""
    return {
        "name": row.get(_entity_col_of(row.get("_record_type", "")), ""),
        "source_chapter": row.get("source_chapter"),
        "summary": _extract_summary(row),
    }


def _extract_summary(row: dict) -> str:
    """从 record 行提取最能代表它的摘要文本（取第一个非空语义字段）。"""
    rt = row.get("_record_type", "")
    summary_fields = {
        "chapter_digest": "summary",
        "scene": "summary",
        "character_state": "status",
        "relationship_state": "relationship_desc",
        "object_state": "condition",
        "plot_promise": "promised_payoff",
        "narrative_function": "summary",
        "world_fact": "fact",
    }
    field = summary_fields.get(rt, "summary")
    val = row.get(field, "")
    return str(val)[:100] if val else ""


def default_packet_formatter(
    anchors: list[dict],
    expanded: list[dict],
    chapter_num: int | None,
) -> str:
    """默认证据包排版（harness 可覆盖，Phase 5）。

    按"角色→关系→场景→伏笔→叙事功能→物品→设定→章节摘要"的叙事优先级排版，
    每条带 evidence_span 引用 + 章节溯源。
    """
    if not anchors and not expanded:
        return ""

    # 按 record_type 分组（anchors 和 expanded 合并后）
    all_records = anchors + expanded
    by_type: dict[str, list[dict]] = {}
    for r in all_records:
        by_type.setdefault(r["_record_type"], []).append(r)

    # 叙事优先级顺序
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
        # 去重（anchors 和 expanded 可能有重复）
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
    """单条 record 的排版（带 evidence 溯源）。"""
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
            import json
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


# ── 进程单例 ────────────────────────────────────────────────────────

_retriever: MemoryRetriever | None = None


def get_memory_retriever() -> MemoryRetriever:
    """获取检索器单例（无状态，用默认 join_rules + formatter）。

    harness 可在 assemble 时注入自定义 retriever（覆盖 join_rules/formatter），
    但 executor 默认用此单例。
    """
    global _retriever
    if _retriever is None:
        _retriever = MemoryRetriever()
    return _retriever


def set_memory_retriever(retriever: MemoryRetriever) -> None:
    """注入自定义检索器（harness assemble 时用，Phase 5）。"""
    global _retriever
    _retriever = retriever


__all__ = [
    "EvidencePacket",
    "MemoryRetriever",
    "default_join_rules",
    "default_packet_formatter",
    "get_memory_retriever",
    "set_memory_retriever",
]
