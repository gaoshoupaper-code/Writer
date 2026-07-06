"""versions API —— 配置版本谱系视图（前端版本谱系页，需求 D8）。

端点（/api/versions 前缀）：
  GET /versions            版本列表（含谱系 + reward + 轮出处）
  GET /versions/{version}  单版本详情（edits + manifest + reward，D11）

区别于 /api/snapshots：
  - snapshots 是执行端/ab_runner 用的原始快照端点（含 tar 语义）。
  - versions 是进化前端用的"谱系 + reward"富化视图，专为驾驶舱/版本谱系页服务。
  - 按 D11，版本详情只展示 edits + manifest + reward，不吐完整 config_json。

数据源：harness_snapshots（谱系）JOIN adapt_rounds（reward + 轮出处）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

import app.core.db as db
from app.versioning import snapshot_repo
from app.versioning import version_changes_repo

logger = logging.getLogger("evolution.versions_api")

router = APIRouter(prefix="/versions", tags=["versions"])


@router.get("")
def list_versions(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """版本列表（按版本号倒序），富化谱系 + reward + 出处的 adapt round。

    每个版本：
      version / parent_version / status / created_at
      change_summary
      reward           — 该版本作为 shipped 时对应的 candidate reward（JOIN adapt_rounds）
      source_session   — 出处的 adapt session_id
      source_round     — 出处的 round
    """
    snaps = snapshot_repo.list_snapshots()

    # 取所有 shipped 轮（建立 version → reward/session 映射）
    shipped_rows = db.query_all(
        """SELECT shipped_version, session_id, round, candidate_scores, critic_verdict
           FROM adapt_rounds WHERE round_outcome='shipped' AND shipped_version IS NOT NULL"""
    ) or []
    shipped_map: dict[int, dict] = {}
    for r in shipped_rows:
        v = r["shipped_version"]
        # candidate_scores 是 list，取 reward 最高的（即被 ship 的那个候选）
        reward = None
        try:
            scores = json.loads(r["candidate_scores"]) if r["candidate_scores"] else []
            reward = max((s.get("reward", 0) for s in scores), default=None)
        except Exception:
            pass
        shipped_map[v] = {"reward": reward, "source_session": r["session_id"], "source_round": r["round"]}

    items = []
    page = snaps[offset: offset + limit]
    for s in page:
        v = s["version"]
        meta = shipped_map.get(v, {})
        items.append({
            "version": v,
            "parent_version": s.get("parent_version"),
            "status": s.get("status"),
            "change_summary": s.get("change_summary"),
            "created_at": s.get("created_at"),
            "source_commit": s.get("source_commit"),
            "reward": meta.get("reward"),
            "source_session": meta.get("source_session"),
            "source_round": meta.get("source_round"),
        })

    return {
        "items": items,
        "total": len(snaps),
        "production_version": next((s["version"] for s in snaps if s.get("status") == "production"), None),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{version}")
def get_version(version: int) -> dict[str, Any]:
    """单版本详情（D11：edits + manifest + reward，不含完整 config_json）。

    edits 来源：该版本作为 shipped 时，对应 adapt_rounds 行的 candidates_json。
    若该版本是 bootstrap 生成（无 adapt 出处），则无 edits，只返回元数据。
    """
    snap = snapshot_repo.get_snapshot(version)
    if snap is None:
        raise HTTPException(404, f"版本 v{version} 不存在")

    # 找出处的 adapt round（取 candidates_json，含被 ship 候选的 edits+manifest）
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
            # 取被 ship 的候选（critic_verdict.ship_idx 或 ranking[0]）
            ship_idx = critic_verdict.get("ship_idx", (critic_verdict.get("ranking") or [0])[0])
            if 0 <= ship_idx < len(cands):
                edits = cands[ship_idx].get("edits", [])
            if 0 <= ship_idx < len(scores):
                reward = scores[ship_idx].get("reward")
            # baseline reward
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
        "source_commit": snap.get("source_commit"),
        "is_bootstrap": round_row is None,
        "edits": edits,
        "reward": reward,
        "baseline_reward": baseline_reward,
        "baseline_version": round_row["baseline_version"] if round_row else None,
        "critic_verdict": critic_verdict,
        "source_session": round_row["session_id"] if round_row else None,
        "source_round": round_row["round"] if round_row else None,
        # 版本差异展示（version_changes 表，D-T12）
        "changes": version_changes_repo.get_changes(version),
    }


__all__ = ["router"]
