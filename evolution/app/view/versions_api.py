"""versions API —— 版本谱系视图（去 DB 重构：谱系从 registry.json，reward 从 adapt_rounds）。

端点（/api/versions 前缀）：
  GET /versions            版本列表（含谱系 + reward + 轮出处）
  GET /versions/{version}  单版本详情（edits + reward）

数据源分工（去 DB 重构）：
  - 版本谱系/元信息 → registry.json（registry_repo）
  - reward/轮出处 → adapt_rounds 表（进化过程数据，留 evolution.db）
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

import app.core.db as db
from app.versioning import registry_repo

logger = logging.getLogger("evolution.versions_api")

router = APIRouter(prefix="/versions", tags=["versions"])


@router.get("")
def list_versions(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """版本列表（按版本号倒序），富化 reward + 出处的 adapt round。

    谱系来自 registry.json；reward 从 adapt_rounds（进化过程数据）JOIN。
    """
    versions = registry_repo.list_versions()

    # 取所有 shipped 轮（建立 version → reward/session 映射）
    shipped_rows = db.query_all(
        """SELECT shipped_version, session_id, round, candidate_scores, critic_verdict
           FROM adapt_rounds WHERE round_outcome='shipped' AND shipped_version IS NOT NULL"""
    ) or []
    shipped_map: dict[int, dict] = {}
    for r in shipped_rows:
        v = r["shipped_version"]
        reward = None
        try:
            scores = json.loads(r["candidate_scores"]) if r["candidate_scores"] else []
            reward = max((s.get("reward", 0) for s in scores), default=None)
        except Exception:
            pass
        shipped_map[v] = {"reward": reward, "source_session": r["session_id"], "source_round": r["round"]}

    items = []
    page = versions[offset: offset + limit]
    for s in page:
        v = s["version"]
        meta = shipped_map.get(v, {})
        items.append({
            "version": v,
            "parent_version": s.get("parent_version"),
            "status": s.get("status"),
            "change_summary": s.get("change_summary"),
            "created_at": s.get("created_at"),
            "reward": meta.get("reward"),
            "source_session": meta.get("source_session") or s.get("source_session"),
            "source_round": meta.get("source_round"),
        })

    return {
        "items": items,
        "total": len(versions),
        "production_version": registry_repo.get_production_version_number(),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{version}")
def get_version(version: int) -> dict[str, Any]:
    """单版本详情（edits + reward，谱系来自 registry）。"""
    snap = registry_repo.get_version(version)
    if snap is None:
        raise HTTPException(404, f"版本 v{version} 不存在")

    # 找出处的 adapt round（reward + candidates 数据，进化过程数据留 DB）
    round_row = db.query_one(
        """SELECT session_id, round, candidates_json, candidate_scores, critic_verdict,
                  baseline_version, baseline_scores
           FROM adapt_rounds WHERE shipped_version=? ORDER BY round DESC LIMIT 1""",
        (version,),
    )

    edits: list[dict[str, Any]] = []
    reward: float | None = None
    baseline_reward: float | None = None
    critic_verdict: dict[str, Any] = {}
    if round_row:
        try:
            cands = json.loads(round_row["candidates_json"]) if round_row["candidates_json"] else []
            scores = json.loads(round_row["candidate_scores"]) if round_row["candidate_scores"] else []
            critic_verdict = json.loads(round_row["critic_verdict"]) if round_row["critic_verdict"] else {}
            ship_idx = critic_verdict.get("ship_idx", (critic_verdict.get("ranking") or [0])[0])
            if 0 <= ship_idx < len(cands):
                edits = cands[ship_idx].get("edits", [])
            if 0 <= ship_idx < len(scores):
                reward = scores[ship_idx].get("reward")
            b_scores = json.loads(round_row["baseline_scores"]) if round_row["baseline_scores"] else {}
            baseline_reward = max(
                (s.get("overall", 0) for s in b_scores.values() if isinstance(s, dict) and not s.get("skipped")),
                default=None,
            )
        except Exception:
            logger.warning("解析 v%s 的 round 数据失败", version, exc_info=True)

    return {
        "version": version,
        "parent_version": snap.get("parent_version"),
        "status": snap.get("status"),
        "change_summary": snap.get("change_summary"),
        "created_at": snap.get("created_at"),
        "is_bootstrap": round_row is None,
        "edits": edits,
        "reward": reward,
        "baseline_reward": baseline_reward,
        "baseline_version": round_row["baseline_version"] if round_row else None,
        "critic_verdict": critic_verdict,
        "source_session": round_row["session_id"] if round_row else snap.get("source_session"),
        "source_round": round_row["round"] if round_row else None,
    }


__all__ = ["router"]
