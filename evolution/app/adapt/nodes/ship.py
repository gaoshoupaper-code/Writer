"""ship 节点 —— 存快照 + git commit + push + reload executor（Phase 8，Task 7.8）。

gate pass 后执行：把候选 config 存为新 harness_snapshots（config_json），
源码 commit（如有新源码）push 到 bare repo，通知 executor reload。

设计依据：决策 #16（热加载）+ D7a（git commit）+ #18（config 快照）。
"""
from __future__ import annotations

import logging

from app.adapt.state import AdaptState

logger = logging.getLogger("evolution.adapt.ship")


def ship(state: AdaptState) -> dict:
    """gate pass 后：存 config 快照 + git push + reload executor。

    Returns: {} (round_outcome 已由 gate 设为 shipped)
    """
    verdict = state.get("critic_verdict", {})
    ship_idx = verdict.get("ship_idx", 0)
    candidates = state.get("candidates", [])

    if ship_idx >= len(candidates):
        logger.error("ship: ship_idx %d 超出范围", ship_idx)
        return {}

    candidate = candidates[ship_idx]
    config = candidate["config"]
    source_commit = candidate.get("source_commit", "")

    # 1. 存 config 快照（决策 #18）
    from app.improvement.snapshot_repo import publish_config
    snap = publish_config(
        config,
        source_commit=source_commit,
        parent_version=state.get("baseline_version"),
        change_summary=f"adapt round {state.get('round', 0)} ship（candidate {ship_idx}）",
    )
    new_version = snap["version"]
    logger.info("ship: config 快照 v%d 已发布", new_version)

    # 2. 记录到 adapt_rounds（E3a 历史）
    _save_round_history(state, new_version)

    # 3. 通知 executor reload（决策 #16）
    _notify_executor_reload()

    logger.info(
        "ship 完成: v%d, reward=%s",
        new_version,
        state.get("candidate_results", [{}])[ship_idx].get("reward", 0) if ship_idx < len(state.get("candidate_results", [])) else "?",
    )
    return {}


def _save_round_history(state: AdaptState, shipped_version: int) -> None:
    """记录本轮到 adapt_rounds（E3a）。"""
    import json
    from datetime import UTC, datetime

    import app.core.db as db

    candidates = state.get("candidates", [])
    candidates_summary = [
        {"edits_count": len(c.get("edits", [])), "source_commit": c.get("source_commit", "")}
        for c in candidates
    ]

    db.execute(
        """INSERT INTO adapt_rounds
           (session_id, round, landscape, candidates_json, round_outcome,
            shipped_version, baseline_version, baseline_scores, candidate_scores, critic_verdict, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            state["session_id"],
            state.get("round", 0),
            state.get("landscape", ""),
            json.dumps(candidates_summary, ensure_ascii=False),
            state.get("round_outcome", ""),
            shipped_version,
            state.get("baseline_version"),
            json.dumps(state.get("baseline_scores", {}), ensure_ascii=False),
            json.dumps(
                [{k: v for k, v in r.items() if k != "scores"} | {"reward": r.get("reward", 0)}
                 for r in state.get("candidate_results", [])],
                ensure_ascii=False,
            ),
            json.dumps(state.get("critic_verdict", {}), ensure_ascii=False),
            datetime.now(UTC).isoformat(),
        ),
    )


def _notify_executor_reload() -> None:
    """HTTP 通知 executor /reload（决策 #16）。"""
    try:
        import httpx
        from app.core.settings import settings

        resp = httpx.post(
            f"{settings.executor_url.rstrip('/')}/internal/reload",
            timeout=10.0,
        )
        if resp.status_code == 200:
            logger.info("executor reload 通知成功: %s", resp.json())
        else:
            logger.warning("executor reload 通知失败: %s %s", resp.status_code, resp.text)
    except Exception:
        logger.warning("executor reload 通知异常（executor 可能未启动）", exc_info=True)


__all__ = ["ship"]
