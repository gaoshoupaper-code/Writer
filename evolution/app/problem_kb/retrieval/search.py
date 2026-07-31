"""相似检索编排（REQ-04 / AC-06 / AC-23 / AC-26 / DEC-10/12/13）。

流水线（DEC-10）：
  ① 结构化过滤：按分类四轴 + lifecycle + severity 缩小范围（SQL WHERE，主库）
  ② FTS5 全文召回：BM25 排序（主库 standard_problems_fts）
  ③ 向量 KNN 重排：sqlite-vec（独立库，可用时）
  ④ RRF 融合：score = Σ 1/(k + rank)，两路融合
  ⑤ 二级排序：验证强度 → 证据质量 → 新鲜度（频率仅解释属性，DEC-12）

返回 Top5 标准问题（REQ-04.2/AC-23），每条标注：
  - 确认状态（已确认标准问题 / 待确认候选）
  - 匹配依据（命中的轴/文本/向量分数）
  - 证据来源（关联实例数）
  - 效果验证阶段（开放/观察中/已控制/已过时）

降级（AC-26/DEC-13）：
  - 向量不可用 → 仅 FTS + 结构化
  - FTS 不可用 → 仅结构化 + LIKE
  - 全失败 → 空结果 + degraded 标志，调用方记录"知识检索不可用"

不调用生成式模型（REQ-04.11/AC-39）；embedding 由调用方按问题组缓存复用。
"""
from __future__ import annotations

import logging
from typing import Any

import app.core.db as db
from app.problem_kb.retrieval import store
from app.problem_kb.taxonomy import UNCLASSIFIED, normalize_axis

logger = logging.getLogger("evolution.problem_kb.search")

# RRF 融合常数（与 executor 端对齐）
_RRF_K = 60
# 默认返回数（REQ-04.2/AC-23：每组最多 5 个）
DEFAULT_TOP_K = 5
# 候选归并阈值（置信度，REQ-03.7）—— 低于此值不作为归并候选，仅低置信度参考
DEFAULT_MERGE_THRESHOLD = 0.55

# lifecycle → 验证强度权重（二级排序用，DEC-12）
# 已控制 > 观察中 > 开放 > 已过时（已控制说明有生产证据支撑）
_LIFECYCLE_STRENGTH = {
    "已控制": 1.0,
    "观察中": 0.7,
    "开放": 0.4,
    "已过时": 0.1,
}


class SearchHit(dict):
    """检索命中（dict 子类，方便字段访问）。

    字段：problem_id, title, score, match_basis, lifecycle, severity,
    formal_frequency, evidence_count, confirmation_status。
    """

    @property
    def problem_id(self) -> str:  # type: ignore[override]
        return self["problem_id"]


class SearchResult:
    """一次检索的结果集。"""

    def __init__(
        self,
        hits: list[SearchHit],
        *,
        degraded: bool = False,
        degraded_reason: str = "",
        embedding_used: bool = False,
    ) -> None:
        self.hits = hits
        self.degraded = degraded  # 是否降级（AC-26）
        self.degraded_reason = degraded_reason
        self.embedding_used = embedding_used  # 本次是否实际调用了 embedding

    @property
    def empty(self) -> bool:
        return not self.hits


def search_similar_problems(
    *,
    query_text: str,
    query_vec: list[float] | None = None,
    classification: dict[str, Any] | None = None,
    top_k: int = DEFAULT_TOP_K,
    exclude_problem_ids: set[str] | None = None,
) -> SearchResult:
    """检索相似标准问题（主入口）。

    Args:
        query_text: 查询文本（当前问题陈述）
        query_vec: 查询向量（调用方按问题组缓存，AC-39）；None 则不做向量重排
        classification: 当前问题分类（用于结构化过滤/加权）
        top_k: 返回数量上限
        exclude_problem_ids: 排除的 problem_id 集合

    Returns:
        SearchResult，hits 按 RRF + 二级排序，最多 top_k 条。
    """
    exclude_problem_ids = exclude_problem_ids or set()
    degraded = False
    reasons: list[str] = []
    embedding_used = False

    # ① 结构化过滤：取候选 problem_id 集合
    candidate_ids = _structural_filter(classification)
    # 结构化过滤失败/无命中 → 候选集为全部标准问题（不强依赖分类）

    # ② FTS5 召回
    fts_hits = store.fts_search(query_text, limit=max(top_k * 4, 20))
    if not fts_hits and query_text.strip():
        # FTS 无命中可能是查询无匹配，也可能是 FTS 不可用；尝试 LIKE 兜底
        fts_hits = _fts_fallback_like(query_text, limit=max(top_k * 4, 20))
        if not fts_hits:
            reasons.append("fts_unavailable")
            degraded = True

    # ③ 向量重排
    vec_hits: list[tuple[str, float]] = []
    if query_vec is not None:
        vec_hits = store.vec_knn(query_vec, k=max(top_k * 4, 20))
        if not vec_hits and not store.is_vector_available():
            reasons.append("vector_unavailable")
            degraded = True
        else:
            embedding_used = True
    elif store.is_vector_available():
        # 向量可用但本次未传 query_vec（调用方选择不向量化）—— 不算降级
        pass

    # ④ RRF 融合
    scored = _rrf_fuse(fts_hits, vec_hits)

    # 若 FTS 和向量都无命中，用结构化候选兜底
    if not scored and candidate_ids:
        scored = [(0.0, pid) for pid in list(candidate_ids)[:top_k * 2]]
        degraded = True
        reasons.append("structural_only")

    # 过滤排除项
    scored = [(s, pid) for s, pid in scored if pid not in exclude_problem_ids]

    # ⑤ 回填元数据 + 二级排序
    hits = _hydrate_and_sort(scored, top_k)

    return SearchResult(
        hits,
        degraded=degraded,
        degraded_reason=";".join(reasons) if reasons else "",
        embedding_used=embedding_used,
    )


# ════════════════════════════════════════════════════════════════
# 内部组件
# ════════════════════════════════════════════════════════════════


def _structural_filter(classification: dict[str, Any] | None) -> set[str]:
    """按分类四轴 + lifecycle 结构化过滤，返回候选 problem_id 集合。

    分类匹配是"软"的：每个轴命中加权，但不过滤掉不匹配的（避免过度收窄）。
    这里返回"任一轴命中"的标准问题作为加权候选。真正的排序由 RRF + 二级完成。
    """
    if not classification:
        return set()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        loc = classification.get("location") or {}
        for field, val in (("affected_mechanism", classification.get("affected_mechanism")),
                           ("failure_nature", classification.get("failure_nature")),
                           ("task_scenario", classification.get("task_scenario"))):
            if val and val != UNCLASSIFIED:
                # classification_json 是 JSON 文本，用 LIKE 模糊匹配（简单可靠）
                clauses.append("classification_json LIKE ?")
                params.append(f'%"{field}": "{normalize_axis(field, val)}"%')
        agent = loc.get("agent")
        if agent and agent != UNCLASSIFIED:
            clauses.append("classification_json LIKE ?")
            params.append(f'%"agent": "{agent}"%')
        if not clauses:
            return set()
        where = " WHERE " + " OR ".join(clauses)
        rows = db.query_all(
            f"SELECT problem_id FROM standard_problems{where}", tuple(params)
        )
        return {r["problem_id"] for r in rows}
    except Exception:
        logger.exception("结构化过滤失败")
        return set()


def _fts_fallback_like(query: str, limit: int = 20) -> list[tuple[str, float]]:
    """FTS5 召回不足时的 LIKE 兜底（补充召回，AC-26）。

    FTS5 trigram 对中文短查询（<3字）或措辞差异敏感，召回率低。本函数用 query 的
    3-gram 滑动切片做 OR LIKE，提高中文召回。每个 3-gram 命中即召回该问题。
    rank 用命中 3-gram 数的负值（命中越多越靠前）。
    """
    if not query.strip():
        return []
    try:
        q = query.strip()
        # 生成 3-gram 子串（中文友好）；短查询直接用原串
        grams: list[str] = []
        if len(q) >= 3:
            for i in range(len(q) - 2):
                g = q[i : i + 3]
                if g not in grams:
                    grams.append(g)
        else:
            grams = [q]
        if not grams:
            return []
        # 取前 6 个 gram 控制查询量
        return _like_search_simple(grams[:6], limit)
    except Exception:
        return []


def _like_search_simple(grams: list[str], limit: int) -> list[tuple[str, float]]:
    """逐 3-gram 做 LIKE 查询并合并计数（rank = -命中数，命中越多越靠前）。"""
    from collections import Counter

    pid_scores: Counter[str] = Counter()
    for g in grams:
        rows = db.query_all(
            """SELECT problem_id FROM standard_problems
               WHERE title LIKE ? OR description LIKE ?""",
            (f"%{g}%", f"%{g}%"),
        )
        for r in rows:
            pid_scores[r["problem_id"]] += 1
    ranked = sorted(pid_scores.items(), key=lambda x: -x[1])[:limit]
    return [(pid, -float(cnt)) for pid, cnt in ranked]


def _rrf_fuse(
    fts_hits: list[tuple[str, float]],
    vec_hits: list[tuple[str, float]],
) -> list[tuple[float, str]]:
    """RRF 融合两路结果。返回 [(score, problem_id)]，按 score 降序。

    score = Σ 1/(k + rank)，rank 从 1 开始。
    """
    fts_rank: dict[str, int] = {}
    for i, (pid, _rank) in enumerate(fts_hits):
        if pid not in fts_rank:
            fts_rank[pid] = i + 1
    vec_rank: dict[str, int] = {}
    for i, (pid, _dist) in enumerate(vec_hits):
        if pid not in vec_rank:
            vec_rank[pid] = i + 1

    all_ids = set(fts_rank) | set(vec_rank)
    scored: list[tuple[float, str]] = []
    for pid in all_ids:
        score = 0.0
        if pid in fts_rank:
            score += 1.0 / (_RRF_K + fts_rank[pid])
        if pid in vec_rank:
            score += 1.0 / (_RRF_K + vec_rank[pid])
        scored.append((score, pid))
    scored.sort(key=lambda x: -x[0])
    return scored


def _hydrate_and_sort(scored: list[tuple[float, str]], top_k: int) -> list[SearchHit]:
    """回填标准问题元数据，做二级排序，截断到 top_k。

    二级排序（DEC-12）：验证强度(lifecycle) → 证据质量(formal_frequency) → 新鲜度。
    RRF 分数作为第一排序键，二级键在同分时打破平局。
    """
    if not scored:
        return []
    pid_to_rrf = {pid: sc for sc, pid in scored}
    ids = list(pid_to_rrf.keys())
    # 批量取标准问题
    placeholders = ",".join("?" * len(ids))
    rows = db.query_all(
        f"SELECT * FROM standard_problems WHERE problem_id IN ({placeholders})",
        tuple(ids),
    )
    # 取每个问题的证据（已确认实例）数
    evidence_counts: dict[str, int] = {}
    if rows:
        e_rows = db.query_all(
            f"""SELECT problem_id, COUNT(*) AS n FROM problem_instance_links
                WHERE problem_id IN ({placeholders}) GROUP BY problem_id""",
            tuple(r["problem_id"] for r in rows),
        )
        evidence_counts = {r["problem_id"]: r["n"] for r in e_rows}

    hits: list[SearchHit] = []
    for r in rows:
        pid = r["problem_id"]
        rrf = pid_to_rrf.get(pid, 0.0)
        lifecycle = r.get("lifecycle_status") or "开放"
        strength = _LIFECYCLE_STRENGTH.get(lifecycle, 0.4)
        evidence_count = evidence_counts.get(pid, 0)
        # 综合分：RRF 主导，二级键作微调（同 RRF 区间内按强度/证据排序）
        composite = rrf * 1000 + strength * 10 + min(evidence_count, 10)
        hit = SearchHit(
            problem_id=pid,
            title=r.get("title") or "",
            rrf_score=round(rrf, 6),
            composite_score=round(composite, 3),
            lifecycle=lifecycle,
            severity=r.get("severity") or UNCLASSIFIED,
            formal_frequency=r.get("formal_frequency") or 0,
            evidence_count=evidence_count,
            confirmation_status="已确认标准问题",  # standard_problems 都是确认后形成的
            match_basis=_describe_match_basis(rrf, bool(evidence_count)),
            effect_stage=_lifecycle_to_effect_stage(lifecycle),
            retrieval_count=r.get("retrieval_count") or 0,
            updated_at=r.get("updated_at") or "",
        )
        hits.append(hit)

    # 排序：composite 降序
    hits.sort(key=lambda h: -h["composite_score"])
    return hits[:top_k]


def _describe_match_basis(rrf: float, has_evidence: bool) -> str:
    """生成人可读的匹配依据描述。"""
    parts = []
    parts.append(f"RRF={rrf:.4f}")
    if has_evidence:
        parts.append("有已确认实例")
    return "；".join(parts)


def _lifecycle_to_effect_stage(lifecycle: str) -> str:
    """lifecycle → 效果验证阶段描述（AC-06 显示用）。"""
    mapping = {
        "开放": "未验证",
        "观察中": "初步有效（候选）",
        "已控制": "生产验证有效",
        "已过时": "已过时",
    }
    return mapping.get(lifecycle, "未验证")


__all__ = [
    "search_similar_problems", "SearchResult", "SearchHit",
    "DEFAULT_TOP_K", "DEFAULT_MERGE_THRESHOLD",
]
