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

import app.core.db as db
from app.improvement import harness_repo, proposer, static_check, ab_stats
from app.diagnosis import calibrate

logger = logging.getLogger("evolution.pipeline")


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
    from app.improvement import replay
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
    from app.diagnosis import evaluation as evaluation_mod
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
    from app.diagnosis import mining
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


# ── Phase 6：surface 级流水线（bounded change，替代整体 harness 重写）──────


def process_signature_surface(signature_id: int) -> dict[str, Any] | None:
    """处理一个失败签名：surface 级 propose + 静态检查（初筛段）。

    与 process_signature（整体 harness）的区别：每次只改一个 surface（bounded change）。

    流程：
      1. 取签名 → resolve_surface_type（决策 D9：签名带 surface_type 或从 target_component 映射）
      2. 取当前 production 该 surface 的 content（从 manifest 查）
      3. propose_surface_with_retry（S14 重试3次，validator 按 content_kind 分发）
      4. save_surface_candidate（status=draft）
      5. static_check 通过 → update_status static_checked，签名 proposed
      6. 失败 → 签名回 open

    Args:
        signature_id: 失败签名 ID

    Returns: {signature_id, surface_type, candidate_version_id, status} 或 None。
    """
    from app.improvement import proposer, surface_repo, manifest_repo

    signature = db.query_one(
        "SELECT * FROM failure_signatures WHERE id=?", (signature_id,)
    )
    if signature is None:
        return None
    if signature["status"] != "open":
        logger.info("签名 %s 状态 %s，跳过", signature_id, signature["status"])
        return None

    # 1. 解析 surface_type + scope
    surface_type = proposer.resolve_surface_type(signature)
    scope = signature.get("surface_scope") or _infer_scope(signature)
    target_ref = signature.get("target_ref", surface_type)

    # 2. 取当前 production 该 surface 的 content（作为改的基础）
    current_content = ""
    parent_version = None
    approved = surface_repo.get_approved_version(surface_type, target_ref, scope)
    if approved is not None:
        current_content = approved["content"]
        parent_version = approved["version"]
    else:
        logger.warning(
            "签名 %s 指向的 surface %s/%s/%s 无 approved 版本，从空开始",
            signature_id, surface_type, target_ref, scope,
        )

    # 标记签名 processing
    db.execute(
        "UPDATE failure_signatures SET status='mining', updated_at=? WHERE id=?",
        (_now(), signature_id),
    )

    # 3. propose + 校验（validator 已在 propose_surface_with_retry 内按 content_kind 分发）
    result = proposer.propose_surface_with_retry(
        signature_id, surface_type, target_ref, scope, current_content,
    )

    if result is None or result.get("content") is None:
        db.execute(
            "UPDATE failure_signatures SET status='open', updated_at=? WHERE id=?",
            (_now(), signature_id),
        )
        return {
            "signature_id": signature_id, "status": "propose_failed",
            "error": result.get("final_error") if result else "unknown",
        }

    content = result["content"]

    # 4. 保存候选 surface 版本
    candidate = proposer.save_surface_candidate(
        signature_id, surface_type, target_ref, scope, content,
        parent_version=parent_version,
        proposer_meta={"attempts": result["attempts"]},
    )

    # 5. static_check 结果记入（validator 已在 propose 内跑过，这里再确认 + 标记）
    from app.improvement import surface_registry
    type_def = surface_registry.get_type_def(surface_type)
    ok, errors = type_def.validator(content, {})
    if ok:
        surface_repo.update_status(
            candidate["id"], "static_checked", static_check_passed=True,
        )
        # 签名标 proposed
        db.execute(
            "UPDATE failure_signatures SET status='proposed', updated_at=? WHERE id=?",
            (_now(), signature_id),
        )
        return {
            "signature_id": signature_id,
            "surface_type": surface_type,
            "surface_name": target_ref,
            "scope": scope,
            "candidate_version_id": candidate["id"],
            "candidate_version": candidate["version"],
            "status": "static_checked",
            "attempts": result["attempts"],
        }
    else:
        surface_repo.update_status(
            candidate["id"], "rejected", static_check_passed=False,
            proposer_meta={**(candidate.get("proposer_meta") or {}),
                           "static_check_errors": errors},
        )
        db.execute(
            "UPDATE failure_signatures SET status='open', updated_at=? WHERE id=?",
            (_now(), signature_id),
        )
        return {
            "signature_id": signature_id, "status": "static_check_failed",
            "errors": errors,
        }


def _infer_scope(signature: dict[str, Any]) -> str:
    """签名无 surface_scope 时，从 target（subagent 名）推断 scope。"""
    target = signature.get("target", "")
    # subagent 名通常就是 scope（interview/storybuilding/detail-outline/writing/meta）
    from app.improvement import surface_registry
    if target in surface_registry.VALID_SCOPES:
        return target
    return surface_registry.SCOPE_GLOBAL


def run_surface_pipeline_cycle() -> dict[str, Any]:
    """后台定期调用：surface 级流水线一轮（Mining → 初筛）。

    替代 run_pipeline_cycle（整体 harness 版）。A/B 段不自动跑。
    """
    from app.diagnosis import mining
    signatures = mining.check_and_mine_all()
    processed = []
    for sig in signatures:
        try:
            result = process_signature_surface(sig["signature_id"])
            processed.append(result)
        except Exception:
            logger.exception("处理签名失败 %s", sig.get("signature_id"))
    return {"mined_signatures": len(signatures), "processed": processed}


def approve_surface_experiment(
    surface_version_id: int, experiment_id: int | None = None,
) -> dict[str, Any] | None:
    """surface A/B 实验批准上线（D17 人工批准后调用）。

    与 approve_experiment（整体 harness）的区别：
      1. surface 标 approved（不是 harness promote）
      2. 触发 manifest_publisher.approve_and_publish（聚合 + 通知）
      3. 签名 resolved

    Args:
        surface_version_id: 胜出的 surface 版本
        experiment_id: 关联的实验记录（可空，手动批准时可能无）
    """
    from app.improvement import manifest_publisher, surface_repo
    # 用 manifest_publisher 完成 approve + publish + notify
    result = manifest_publisher.approve_and_publish(surface_version_id)
    if result is None:
        return None

    # 签名 resolved
    ver = surface_repo.get_version_by_id(surface_version_id)
    if ver and ver["signature_id"]:
        db.execute(
            "UPDATE failure_signatures SET status='resolved', updated_at=? WHERE id=?",
            (_now(), ver["signature_id"]),
        )
    return result


# ── Phase 6 T3.6：surface 级 A/B（scope 级过滤，决策 D9）─────────────────


# scope（surface 归属）→ evaluation_scores.target（评估维度名）映射。
# A/B 只评受影响 scope 的产出（如改 writing prompt，只评 writing 维度分）。
# target 含 subagent 名（writing/storybuilding/detail-outline）和 content（全局内容质量）。
_SCOPE_TO_EVAL_TARGETS: dict[str, list[str]] = {
    "storybuilding": ["storybuilding"],
    "detail-outline": ["detail-outline"],
    "writing": ["writing"],
    "interview": ["interview"],
    # meta 改动影响编排全链路 → 评所有维度（含 content 全局质量）
    "meta": ["content", "storybuilding", "detail-outline", "writing"],
    # global 改动影响全局 → 全量评
    "global": ["content", "storybuilding", "detail-outline", "writing"],
}


def run_surface_ab_experiment(
    candidate_surface_version_id: int,
    test_set_id: int | None = None,
    *,
    run_generation_fn: Callable[[str, int, dict], str] | None = None,
    seed_count: int | None = None,
) -> dict[str, Any] | None:
    """对候选 surface 版本跑 A/B（scope 级过滤，决策 D9）。

    与 run_ab_experiment（整体 harness）的区别：
      1. 候选是单个 surface 版本（不是整个 harness）
      2. 回放时用「替换该 surface 的 manifest」vs production manifest
      3. 分数收集按 scope 过滤（只评受影响维度），bounded change 的成本优势

    scope 全量规则（设计 D9）：
      - A/B 类 surface：scope 级过滤（只评受影响维度）
      - C 类 surface（改 schema）：强制全量（schema 影响全局）

    Args:
        candidate_surface_version_id: 候选 surface_versions.id
        test_set_id: 测试集（None 用 default-multistyle）
        run_generation_fn: 生成执行函数 (request, manifest_version, item) → trace_id
        seed_count: N（None 从 calibrate 取）

    Returns: 统计结果 dict。
    """
    from app.improvement import surface_repo, surface_registry, manifest_repo
    from app.diagnosis import calibrate

    candidate = surface_repo.get_version_by_id(candidate_surface_version_id)
    if candidate is None:
        return None

    scope = candidate["scope"]
    surface_type = candidate["surface_type"]
    is_c_code = surface_registry.is_c_code(surface_type)

    # C 类强制全量评估（schema 影响全局）；A/B 类 scope 级过滤
    eval_targets = None if is_c_code else _SCOPE_TO_EVAL_TARGETS.get(scope)

    prod_manifest = manifest_repo.get_production_manifest()
    if prod_manifest is None:
        logger.error("无 production manifest，无法 A/B")
        return None

    N = seed_count or calibrate.get_max_n_for_experiment()
    test_set = _get_test_set(test_set_id)
    if test_set is None:
        return None
    test_items = test_set["prompts"]

    now = _now()
    cur = db.execute(
        """INSERT INTO harness_experiments
           (candidate_version, prod_version, signature_id, test_set_id,
            seed_count, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'running', ?)""",
        (candidate["version"], prod_manifest["manifest_version"],
         candidate.get("signature_id"), test_set["id"], N, now),
    )
    exp_id = cur.lastrowid

    # surface 版 A/B 的 prod/cand 对比：
    #   prod = 当前 production manifest 跑
    #   cand = 「production manifest 但该 surface 换成候选版本」跑
    # 实际部署时 run_generation_fn 负责构造 cand manifest（替换该 surface 指针）。
    # 此处只做编排逻辑，实际生成由 run_fn 调 backend。
    prod_scores: list[float] = []
    cand_scores: list[float] = []
    if run_generation_fn is not None:
        try:
            for item in test_items:
                for _ in range(N):
                    # prod 跑（当前 production manifest）
                    prod_trace = run_generation_fn(
                        item.get("request", ""), prod_manifest["manifest_version"], item,
                    )
                    ps = _get_trace_avg_score_filtered(prod_trace, eval_targets)
                    if ps is not None:
                        prod_scores.append(ps)
                    # cand 跑（用 -1 标记 candidate surface 版本，执行端识别后替换该 surface）
                    cand_trace = run_generation_fn(
                        item.get("request", ""), -candidate_surface_version_id, item,
                    )
                    cs = _get_trace_avg_score_filtered(cand_trace, eval_targets)
                    if cs is not None:
                        cand_scores.append(cs)
        except Exception as exc:
            logger.exception("surface A/B 生成失败")
            _finish_experiment(exp_id, status="error")
            return {"experiment_id": exp_id, "status": "error", "error": str(exc)}
    else:
        logger.info("surface A/B %s 待执行（run_generation_fn 未提供）", exp_id)
        surface_repo.update_status(candidate["id"], "ab_testing")
        return {"experiment_id": exp_id, "status": "pending_execution",
                "seed_count": N, "scope": scope, "is_c_code": is_c_code,
                "eval_targets": eval_targets}

    # 统计判定
    stats = ab_stats.decide_verdict(prod_scores, cand_scores)
    _save_experiment_results(exp_id, prod_scores, cand_scores, stats)
    surface_repo.update_status(candidate["id"], "ab_testing")
    return {
        "experiment_id": exp_id, "verdict": stats["verdict"],
        "confidence": stats["confidence"], "seed_count": N,
        "scope": scope, "is_c_code": is_c_code, "eval_targets": eval_targets,
        "prod_mean": stats["mean_prod"], "cand_mean": stats["mean_cand"],
    }


def _get_trace_avg_score_filtered(
    trace_id: str, eval_targets: list[str] | None,
) -> float | None:
    """取一个 trace 的评估均分（可按 target 过滤）。

    eval_targets=None：取所有维度均分（全量，C 类用）。
    eval_targets=[...]：只取这些 target（subagent 名）的维度均分（scope 级过滤）。
    """
    if eval_targets is None:
        return _get_trace_avg_score(trace_id)
    placeholders = ",".join("?" * len(eval_targets))
    rows = db.query_all(
        f"SELECT score FROM evaluation_scores WHERE trace_id=? AND target IN ({placeholders})",
        (trace_id, *eval_targets),
    )
    if not rows:
        return None
    return sum(r["score"] for r in rows) / len(rows)
