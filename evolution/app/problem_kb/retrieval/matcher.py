"""候选归并生成（REQ-03 / AC-41 / DEC-05/06/28）。

对新收录的问题实例检索相似标准问题，按校准阈值生成候选归并：
  - 有标准问题达到阈值 → 生成"归并已有标准问题"候选（REQ-03.1）
  - 无标准问题达到阈值但 Top5 有相关 → 仅低置信度参考（REQ-03.7）
  - 无任何相似标准问题 → 生成"新标准问题候选"（is_new_problem_proposal=True，REQ-03.2/AC-41）

匹配版本留痕（AC-45）：每个候选记录 match_method/match_model_version/confidence/match_evidence，
匹配能力升级可重新生成新候选，但不改写已确认关系（REQ-03.6）。

非阻塞（AC-14）：失败仅记日志，实例事实已保留。

注意：候选生成不调用 embedding（收录时无 query_vec，避免每个 finding 都触发 embedding
调用）。向量重排只在进化检索时按问题组进行（AC-39）。这里仅做 FTS + 结构化匹配。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from app.problem_kb import repo
from app.problem_kb.retrieval import search, store
from app.problem_kb.retrieval.embedder import get_embedder

logger = logging.getLogger("evolution.problem_kb.matcher")

# 候选生成用的检索方法标识（AC-45 留痕）
_MATCH_METHOD = "structural_fts"
# 置信度估算：FTS 命中 + 结构化匹配的组合分（0..1）
# 简化模型：RRF top1 的相对分 + 结构化轴命中加成
_BASE_CONFIDENCE = 0.5
_AXIS_BONUS = 0.08  # 每个分类轴命中加成
_EVIDENCE_BONUS = 0.05  # 有已确认实例加成
_TOP_N_FOR_MATCH = 5  # 候选生成看 Top5（REQ-03.7）


def generate_candidates(
    *,
    instance_id: str,
    finding: dict[str, Any],
    classification: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """为一个新实例生成候选归并。返回创建的 candidate_id 列表。

    Args:
        instance_id: 问题实例 id
        finding: 评估 finding（含 statement/evidence）
        classification: 实例的多轴分类
        conn: 可选，与封存同事务时传入
    """
    candidate_ids: list[str] = []
    try:
        statement = str(finding.get("finding") or "")
        evidence_text = str(finding.get("evidence") or "")
        query_text = f"{statement} {evidence_text}".strip()
        if not query_text:
            return []

        # 检索相似标准问题（不带向量，收录时不调 embedding）
        result = search.search_similar_problems(
            query_text=query_text,
            query_vec=None,  # 收录阶段不做向量重排
            classification=classification,
            top_k=_TOP_N_FOR_MATCH,
        )

        match_version = _current_match_version()

        if result.empty:
            # 无任何相似 → 新标准问题候选（REQ-03.2/AC-41）
            cand_id = repo.create_candidate(
                instance_id=instance_id,
                target_problem_id=None,
                is_new_problem_proposal=True,
                match_method=_MATCH_METHOD,
                match_model_version=match_version,
                confidence=0.0,
                match_evidence="无相似历史标准问题，提议新建",
                conn=conn,
            )
            candidate_ids.append(cand_id)
            return candidate_ids

        # 有命中：按阈值区分归并候选 vs 低置信度参考
        any_above_threshold = False
        for hit in result.hits:
            confidence = _estimate_confidence(hit, classification)
            if confidence >= search.DEFAULT_MERGE_THRESHOLD:
                any_above_threshold = True
                cand_id = repo.create_candidate(
                    instance_id=instance_id,
                    target_problem_id=hit["problem_id"],
                    is_new_problem_proposal=False,
                    match_method=_MATCH_METHOD,
                    match_model_version=match_version,
                    confidence=confidence,
                    match_evidence=_build_match_evidence(hit, classification),
                    conn=conn,
                )
                candidate_ids.append(cand_id)

        # 没有任何达到阈值 → 新标准问题候选（REQ-03.7：低于阈值的 Top5 只作低置信度参考，
        # 不作为归并候选；此时生成新标准问题候选）
        if not any_above_threshold:
            cand_id = repo.create_candidate(
                instance_id=instance_id,
                target_problem_id=None,
                is_new_problem_proposal=True,
                match_method=_MATCH_METHOD,
                match_model_version=match_version,
                confidence=result.hits[0]["composite_score"] if result.hits else 0.0,
                match_evidence=(
                    f"最相似标准问题置信度低于阈值({search.DEFAULT_MERGE_THRESHOLD})，"
                    f"top1={result.hits[0]['title'] if result.hits else '无'}"
                ),
                conn=conn,
            )
            candidate_ids.append(cand_id)

    except Exception:
        logger.exception("候选归并生成异常 instance=%s（已忽略）", instance_id)
    return candidate_ids


def _current_match_version() -> str:
    """当前匹配方法/模型版本（AC-45 留痕）。

    含检索方法 + embedding 模型（若可用）+ FTS 版本，便于跨版本回放基准。
    """
    embedder = get_embedder()
    embed_model = embedder.model_version if embedder else "disabled"
    return f"{_MATCH_METHOD}|embed={embed_model}|fts=trigram"


def _estimate_confidence(hit: dict[str, Any], classification: dict[str, Any]) -> float:
    """估算候选置信度（0..1）。

    简化模型：基础分 + 分类轴命中加成 + 证据加成，截断到 [0, 0.95]。
    真实阈值校准由阶段 D 的 50 条基准完成（AC-47），本期用启发式默认值。
    """
    conf = _BASE_CONFIDENCE
    # 分类轴命中加成
    cls_json = hit.get("classification_json") if isinstance(hit, dict) else None
    # hit 来自 _hydrate_and_sort，没有 classification_json，用 lifecycle/证据近似
    if hit.get("evidence_count", 0) > 0:
        conf += _EVIDENCE_BONUS * min(hit["evidence_count"], 3)
    # RRF 分贡献（归一化）
    rrf = hit.get("rrf_score", 0.0)
    conf += min(rrf * 0.5, 0.2)
    return min(conf, 0.95)


def _build_match_evidence(hit: dict[str, Any], classification: dict[str, Any]) -> str:
    """构建人可读的匹配依据（AC-06/AC-45）。"""
    parts = [
        f"匹配问题: {hit.get('title', '?')}",
        f"RRF={hit.get('rrf_score', 0):.4f}",
        f"已确认实例数={hit.get('evidence_count', 0)}",
        f"生命周期={hit.get('lifecycle', '?')}",
    ]
    return "；".join(parts)


__all__ = ["generate_candidates"]
