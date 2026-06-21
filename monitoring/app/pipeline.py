"""流水线编排（Phase 4 T4.5，S12 分段自动化 + S13 A/B 串行队列）。

把签名 → proposer → 静态检查 → A/B → 批准串成状态机。
状态流转（harness_versions.status）：
  draft → static_checked → ab_testing → (pending_approval) → approved
  任一环节失败 → rejected

分段自动化（S12）：
  - 初筛段（自动）：签名→proposer→静态检查
  - A/B 段（排队）：N seed 对比（S13 串行）
  - 批准段（人工）：D17 人工批准

A/B 实际执行（起容器跑生成）依赖 Docker + 真 LLM，用可注入的 run_ab_fn
解耦。本模块只做编排逻辑 + 状态推进 + 统计汇总。

设计依据：设计文档 S12/S13/D6/D10/D17 + harness_versions 状态机。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable

import app.db as db
from app import harness_repo, proposer, static_check, ab_stats, calibrate

logger = logging.getLogger("monitoring.pipeline")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 初筛段（自动连锁）：签名 → proposer → 静态检查 ──────────


def process_signature(
    signature_id: int,
    harnesses_root: str,
    *,
    validate_fn: Callable[[str], tuple[bool, str]] | None = None,
) -> dict[str, Any] | None:
    """处理一个失败签名：propose + 静态检查（初筛段）。

    流程：
      1. 取签名 + 当前 production harness 代码
      2. propose_with_retry（S14，带校验重试3次）
      3. 静态检查（D10）
      4. 过则建 harness_versions 记录（status=static_checked），签名为 proposed
      5. 不过则签名为 open（等更多数据或重试）

    validate_fn: proposer 的代码校验函数（实际接 worker.load_harness_instance）。
                 若为 None，用 static_check 作为校验。
    """
    signature = db.query_one(
        "SELECT * FROM failure_signatures WHERE id=?", (signature_id,)
    )
    if signature is None:
        return None
    if signature["status"] != "open":
        logger.info("签名 %s 状态 %s，跳过", signature_id, signature["status"])
        return None

    # 取当前 production harness 代码
    prod = harness_repo.get_production_version()
    if prod is None:
        logger.error("无 production harness，无法 propose")
        return None
    current_code = harness_repo.read_harness_code(prod["code_path"])

    # 标记签名 processing
    db.execute(
        "UPDATE failure_signatures SET status='mining', updated_at=? WHERE id=?",
        (_now(), signature_id),
    )

    # propose（若 validate_fn 为 None，用 static_check 作校验）
    checker = validate_fn or _static_check_as_validator
    result = proposer.propose_with_retry(
        signature_id, current_code, validate_fn=checker,
    )

    if result is None or result.get("code") is None:
        # 全部失败，签名回 open
        db.execute(
            "UPDATE failure_signatures SET status='open', updated_at=? WHERE id=?",
            (_now(), signature_id),
        )
        return {"signature_id": signature_id, "status": "propose_failed",
                "error": result.get("final_error") if result else "unknown"}

    code = result["code"]

    # 再做一次完整静态检查（双重保险）
    ok, errors = static_check.static_check(code)
    if not ok:
        db.execute(
            "UPDATE failure_signatures SET status='open', updated_at=? WHERE id=?",
            (_now(), signature_id),
        )
        return {"signature_id": signature_id, "status": "static_check_failed",
                "errors": errors}

    # 保存候选 harness 版本
    candidate = proposer.save_candidate(
        signature_id, code, harnesses_root,
        parent_version=prod["version"],
        proposer_meta={"attempts": result["attempts"]},
    )
    harness_repo.update_status(candidate["id"], "static_checked")

    # 签名标记 proposed
    db.execute(
        "UPDATE failure_signatures SET status='proposed', updated_at=? WHERE id=?",
        (_now(), signature_id),
    )

    return {
        "signature_id": signature_id,
        "candidate_version": candidate["version"],
        "candidate_id": candidate["id"],
        "status": "static_checked",
        "attempts": result["attempts"],
    }


def _static_check_as_validator(code: str) -> tuple[bool, str]:
    """用 static_check 作为 proposer 校验函数（无 worker 时的降级）。"""
    ok, errors = static_check.static_check(code)
    return ok, "; ".join(errors) if errors else ""


# ── A/B 段（排队 + 执行）────────────────────────────────────


def run_ab_experiment(
    candidate_version: int,
    test_set_id: int | None = None,
    *,
    run_generation_fn: Callable[[str, str, dict], str] | None = None,
    seed_count: int | None = None,
) -> dict[str, Any] | None:
    """对候选 harness 跑 A/B 实验（N seed × production/candidate）。

    Args:
        candidate_version: 候选 harness 版本号
        test_set_id: 测试集（None 用 default-multistyle）
        run_generation_fn: 生成执行函数（prompt_label, test_item) → trace_id）。
                           实际接 backend 的 /internal/ab-replay。若 None 则跳过实际生成。
        seed_count: N（None 从 calibrate 取）

    完整统计量（S11）：prod/cand 各 N 分数 → t 检验 → verdict。
    """
    candidate = harness_repo.get_version(candidate_version)
    if candidate is None:
        return None
    prod = harness_repo.get_production_version()
    if prod is None:
        return None

    N = seed_count or calibrate.get_max_n_for_experiment()
    test_set = _get_test_set(test_set_id)
    if test_set is None:
        return None
    test_items = test_set["prompts"]

    # 建 experiment 记录
    now = _now()
    cur = db.execute(
        """INSERT INTO harness_experiments
           (candidate_version, prod_version, signature_id, test_set_id,
            seed_count, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'running', ?)""",
        (candidate_version, prod["version"], candidate.get("signature_id"),
         test_set["id"], N, now),
    )
    exp_id = cur.lastrowid

    # 实际生成（若提供了 run_generation_fn）
    prod_scores: list[float] = []
    cand_scores: list[float] = []
    if run_generation_fn is not None:
        try:
            prod_scores, cand_scores = _collect_scores(
                run_generation_fn, test_items, N,
                prod_version=prod["version"],
                cand_version=candidate_version,
            )
        except Exception as exc:
            logger.exception("A/B 生成失败")
            _finish_experiment(exp_id, status="error")
            return {"experiment_id": exp_id, "status": "error", "error": str(exc)}
    else:
        # 无生成函数：记录待执行（实际部署时由后台 worker 跑）
        logger.info("A/B 实验 %s 待执行（run_generation_fn 未提供）", exp_id)
        return {"experiment_id": exp_id, "status": "pending_execution", "seed_count": N}

    # 统计判定
    stats = ab_stats.decide_verdict(prod_scores, cand_scores)
    _save_experiment_results(exp_id, prod_scores, cand_scores, stats)
    harness_repo.update_status(candidate["id"], "ab_testing")

    return {
        "experiment_id": exp_id, "verdict": stats["verdict"],
        "confidence": stats["confidence"], "seed_count": N,
        "prod_mean": stats["mean_prod"], "cand_mean": stats["mean_cand"],
    }


def _get_test_set(test_set_id: int | None) -> dict[str, Any] | None:
    """取测试集（None 用 default-multistyle，回退 default-xianxia）。"""
    from app import replay
    if test_set_id is not None:
        return replay.get_test_set(test_set_id)
    ts = replay.ensure_default_multistyle_test_set()
    return ts


def _collect_scores(
    run_fn: Callable, test_items: list[dict], N: int,
    *, prod_version: int, cand_version: int,
) -> tuple[list[float], list[float]]:
    """跑 N seed × 测试集，收集分数。

    run_fn(test_item, version) → trace_id → 查 evaluation_scores 取分。
    实际部署时 run_fn 调 backend /internal/ab-replay。
    """
    from app import evaluation as evaluation_mod
    prod_scores: list[float] = []
    cand_scores: list[float] = []
    for item in test_items:
        for _ in range(N):
            for label, version, sink in (
                ("prod", prod_version, prod_scores),
                ("cand", cand_version, cand_scores),
            ):
                trace_id = run_fn(item.get("request", ""), version, item)
                # 查该 trace 的评估均分
                score = _get_trace_avg_score(trace_id)
                if score is not None:
                    sink.append(score)
    return prod_scores, cand_scores


def _get_trace_avg_score(trace_id: str) -> float | None:
    """取一个 trace 的所有评估维度均分。"""
    rows = db.query_all(
        "SELECT score FROM evaluation_scores WHERE trace_id=?", (trace_id,)
    )
    if not rows:
        return None
    return sum(r["score"] for r in rows) / len(rows)


def _save_experiment_results(
    exp_id: int, prod_scores: list[float], cand_scores: list[float],
    stats: dict[str, Any],
) -> None:
    """存 A/B 完整结果到 harness_experiments。"""
    db.execute(
        """UPDATE harness_experiments SET
           prod_scores_json=?, cand_scores_json=?,
           prod_mean=?, prod_std=?, cand_mean=?, cand_std=?,
           ci_low=?, ci_high=?, p_value=?,
           verdict=?, confidence=?, status='done', finished_at=?
           WHERE id=?""",
        (
            json.dumps(prod_scores), json.dumps(cand_scores),
            stats["mean_prod"], stats["std_prod"],
            stats["mean_cand"], stats["std_cand"],
            stats["ci_low"], stats["ci_high"], stats["p_value_approx"],
            stats["verdict"], stats["confidence"], _now(), exp_id,
        ),
    )


def _finish_experiment(exp_id: int, status: str) -> None:
    db.execute(
        "UPDATE harness_experiments SET status=?, finished_at=? WHERE id=?",
        (status, _now(), exp_id),
    )


# ── 批准段（D17 人工）──────────────────────────────────────


def approve_experiment(experiment_id: int) -> dict[str, Any] | None:
    """人工批准 A/B 实验胜出的候选上线（D17）。

    流程：candidate harness 升 production label，原 production 降级。
    """
    exp = db.query_one("SELECT * FROM harness_experiments WHERE id=?", (experiment_id,))
    if exp is None:
        return None
    if exp["verdict"] != "win":
        raise ValueError(f"只有 verdict=win 的实验可批准，当前: {exp['verdict']}")

    candidate = harness_repo.get_version(exp["candidate_version"])
    if candidate is None:
        return None

    # candidate 升 production
    harness_repo.promote_to_production(candidate["id"])
    # experiment 标记 approved
    db.execute(
        "UPDATE harness_experiments SET status='approved' WHERE id=?",
        (experiment_id,),
    )
    # 签名 resolved
    if exp["signature_id"]:
        db.execute(
            "UPDATE failure_signatures SET status='resolved', updated_at=? WHERE id=?",
            (_now(), exp["signature_id"]),
        )
    return {"experiment_id": experiment_id, "status": "approved",
            "candidate_version": exp["candidate_version"]}


def reject_experiment(experiment_id: int) -> dict[str, Any] | None:
    """拒绝候选（verdict=lose/tie 或人工判断不上线）。"""
    exp = db.query_one("SELECT * FROM harness_experiments WHERE id=?", (experiment_id,))
    if exp is None:
        return None
    candidate = harness_repo.get_version(exp["candidate_version"])
    if candidate:
        harness_repo.update_status(candidate["id"], "rejected")
    db.execute(
        "UPDATE harness_experiments SET status='rejected' WHERE id=?",
        (experiment_id,),
    )
    # 签名回 open（等更多数据或换策略）
    if exp["signature_id"]:
        db.execute(
            "UPDATE failure_signatures SET status='open', updated_at=? WHERE id=?",
            (_now(), exp["signature_id"]),
        )
    return {"experiment_id": experiment_id, "status": "rejected"}


# ── 全自动触发（后台）──────────────────────────────────────


def run_pipeline_cycle(harnesses_root: str) -> dict[str, Any]:
    """后台定期调用：跑一轮完整流水线（Mining → 初筛 → 待 A/B）。

    A/B 段不自动跑（成本高，需排队或人工触发）。
    """
    from app import mining
    # 1. Mining（若有新签名攒够）
    signatures = mining.check_and_mine_all()
    # 2. 对每个 open 签名跑初筛
    processed = []
    for sig in signatures:
        try:
            result = process_signature(sig["signature_id"], harnesses_root)
            processed.append(result)
        except Exception:
            logger.exception("处理签名失败 %s", sig.get("signature_id"))
    return {"mined_signatures": len(signatures), "processed": processed}
