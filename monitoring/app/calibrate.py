"""Phase 0 T0.1：judge 方差校准（D22 前置子任务）。

目的：LLM-judge 对同一输入多次评分有方差（temperature>0 或模型自身波动）。
方差的科学依据 → 定 A/B 实验的 seed 数 N（σ 大则 N 大）。
不校准就拍 N，统计验证不可信（D6 前置依赖）。

做法：
  - 对固定样本（内容层正文 + 4 subagent 交付物），每类调 M 次 judge
  - 收集每维度的分数序列 → 算均值/标准差 σ
  - 按两样本 t 检验样本量公式算推荐 N（per group）
  - 存 judge_calibration 表，experiment.py 读此表取 N

N 公式（两独立样本均值比较，α=0.05 双侧, 功效 0.8, 最小可检测差异 δ=0.1）：
  N_per_group = ceil(2 * ((z_α/2 + z_β) * σ / δ)²)
  z_α/2=1.96, z_β=0.84 → 系数 2.8
  N 兜底在 [5, 40] 之间（σ 太小给最低 5，太大封顶 40 避免成本失控）

设计依据：需求 D22（前置校准）+ 设计 S2（双层都校准）。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

import app.db as db
from app import llm
from app import evaluation as evaluation_mod
from app.rubrics import xianxia as rubric

logger = logging.getLogger("monitoring.calibrate")

# 校准默认跑多少次（M）
DEFAULT_SAMPLE_COUNT = 20
# 统计参数
_ALPHA_Z = 1.96   # α=0.05 双侧
_BETA_Z = 0.84    # 功效 0.8
_DELTA = 0.1      # 最小可检测差异（0.1 分）
# N 兜底范围
_N_MIN = 5
_N_MAX = 40


def recommend_seed_count(std: float) -> int:
    """据标准差 σ 算推荐 seed 数 N（per group）。

    两独立样本均值比较的样本量公式，检测均值差 ≥ δ 的显著性。
    """
    if std <= 0:
        return _N_MIN
    n = 2 * ((_ALPHA_Z + _BETA_Z) * std / _DELTA) ** 2
    n = math.ceil(n)
    return max(_N_MIN, min(_N_MAX, n))


def calibrate_dimension(
    messages: list[dict[str, str]],
    metric_key: str,
    layer: str,
    target: str,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
) -> dict[str, Any] | None:
    """对单个 judge 维度跑 M 次，算方差。

    Args:
        messages: 完整的 judge messages（system: rubric+format, user: 样本文本）
        metric_key: 要收集的维度键（内容层可能是多个，这里取一个）
        layer/target/metric: 写库用
        sample_count: M

    Returns: 校准结果 dict 或 None（失败）。
    """
    scores: list[float] = []
    for i in range(sample_count):
        try:
            # temperature>0 引入方差（judge 默认 temp=0，这里用 0.3 模拟真实波动）
            raw = llm.chat(messages, temperature=0.3)
            result = evaluation_mod._parse_response(raw)
            scores_dict = result.get("scores", {})
            # 内容层多维度时取指定键；subagent 单维度取该键或 overall
            score = scores_dict.get(metric_key, result.get("overall"))
            if score is None:
                logger.warning("校准第 %d 次：维度 %s 无分数，跳过", i + 1, metric_key)
                continue
            scores.append(float(score))
        except Exception:
            logger.exception("校准第 %d 次失败 %s", i + 1, metric_key)

    if len(scores) < 3:
        logger.error("校准 %s 有效样本不足（%d/%d）", metric_key, len(scores), sample_count)
        return None

    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = math.sqrt(variance)
    n = recommend_seed_count(std)
    return {
        "layer": layer,
        "target": target,
        "metric": metric_key,
        "sample_count": len(scores),
        "scores": scores,
        "mean": mean,
        "std": std,
        "recommended_n": n,
    }


def save_calibration(result: dict[str, Any]) -> None:
    """存校准结果到 judge_calibration 表（覆盖同维度旧记录）。"""
    now = datetime.now(UTC).isoformat()
    with db._lock:
        conn = db.get_conn()
        # 删除同维度旧记录（校准可重跑，取最新）
        conn.execute(
            "DELETE FROM judge_calibration WHERE layer=? AND target=? AND metric=?",
            (result["layer"], result["target"], result["metric"]),
        )
        conn.execute(
            """INSERT INTO judge_calibration
               (layer, target, metric, sample_count, scores_json, mean, std, recommended_n, calibrated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["layer"], result["target"], result["metric"],
                result["sample_count"], json.dumps(result["scores"]),
                result["mean"], result["std"], result["recommended_n"], now,
            ),
        )
        conn.commit()


def calibrate_all(
    content_sample: str,
    subagent_samples: dict[str, str],
    sample_count: int = DEFAULT_SAMPLE_COUNT,
) -> dict[str, Any]:
    """完整双层校准。

    Args:
        content_sample: 内容层评估样本（一段正文）
        subagent_samples: {agent_name: 交付物样本文本}
        sample_count: M

    Returns: {layer: [calibration_result, ...]}
    """
    if not llm.judge_enabled():
        raise RuntimeError("LLM-judge 未配置（JUDGE_MODEL/JUDGE_API_KEY 为空）")

    results: dict[str, list[dict[str, Any]]] = {"content": [], "subagent": []}

    # 内容层：6 维度，一次 judge 调用出 6 个分数 → 对每个维度算 σ
    rubric_prompt = rubric.build_content_rubric_prompt()
    output_format = rubric.build_output_format(rubric.content_dim_keys())
    content_messages = [
        {"role": "system", "content": rubric_prompt + output_format},
        {"role": "user", "content": f"## 待评估作品正文\n\n{content_sample}"},
    ]
    # 内容层一次调用出 6 维度，跑 M 次后对每维度分别算 σ
    content_scores: dict[str, list[float]] = {k: [] for k in rubric.content_dim_keys()}
    for i in range(sample_count):
        try:
            raw = llm.chat(content_messages, temperature=0.3)
            parsed = evaluation_mod._parse_response(raw)
            scores_dict = parsed.get("scores", {})
            for key in rubric.content_dim_keys():
                if key in scores_dict:
                    content_scores[key].append(float(scores_dict[key]))
        except Exception:
            logger.exception("内容层校准第 %d 次失败", i + 1)

    for key, scores in content_scores.items():
        if len(scores) < 3:
            logger.warning("内容维度 %s 样本不足 %d", key, len(scores))
            continue
        mean = sum(scores) / len(scores)
        std = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))
        n = recommend_seed_count(std)
        result = {
            "layer": "content", "target": "novel", "metric": key,
            "sample_count": len(scores), "scores": scores,
            "mean": mean, "std": std, "recommended_n": n,
        }
        save_calibration(result)
        results["content"].append(result)

    # subagent 层：每个 subagent 单维度，各跑 M 次
    for dim in rubric.SUBAGENT_DIMENSIONS:
        agent = dim["agent"]
        key = dim["key"]
        sample_text = subagent_samples.get(agent)
        if not sample_text:
            logger.warning("subagent %s 无校准样本，跳过", agent)
            continue
        sub_rubric = rubric.build_subagent_rubric_prompt(agent)
        sub_format = rubric.build_output_format([key])
        messages = [
            {"role": "system", "content": sub_rubric + sub_format},
            {"role": "user", "content": f"## {agent} 环节交付物\n\n{sample_text}"},
        ]
        result = calibrate_dimension(messages, key, "subagent", agent, sample_count)
        if result:
            save_calibration(result)
            results["subagent"].append(result)

    return results


def get_recommended_n(layer: str, target: str, metric: str) -> int:
    """读校准表取某维度的推荐 seed 数 N（未校准回退默认值）。"""
    row = db.query_one(
        "SELECT recommended_n FROM judge_calibration WHERE layer=? AND target=? AND metric=?",
        (layer, target, metric),
    )
    if row:
        return int(row["recommended_n"])
    # 未校准回退（保守默认 10，与 Mining N 一致）
    return 10


def get_max_n_for_experiment() -> int:
    """取一次 A/B 实验用的 N：所有已校准维度的推荐 N 的最大值。

    A/B 比较的是综合分（多维度均值），N 取最大保证所有维度都可信。
    """
    rows = db.query_all("SELECT recommended_n FROM judge_calibration")
    if not rows:
        return 10
    return max(int(r["recommended_n"]) for r in rows)


def list_calibrations() -> list[dict[str, Any]]:
    """列所有校准结果（供 API/查看）。"""
    rows = db.query_all(
        "SELECT layer, target, metric, sample_count, mean, std, recommended_n, calibrated_at "
        "FROM judge_calibration ORDER BY layer, target"
    )
    return [dict(r) for r in rows]
