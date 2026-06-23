"""A/B 统计计算（Phase 4 T4.4，D6 N seed + S11 完整统计量）。

纯统计函数，供 experiment 编排调用。两独立样本均值比较：
  - 均值/标准差
  - 置信区间（候选均值的 CI）
  - p 值近似（Welch t 检验）
  - verdict 判定（win/lose/tie，基于置信区间与 baseline 均值的分离）

verdict 逻辑（S11，非简单均值差 ±0.05）：
  - win: 候选 CI 下界 > production 均值（候选显著更优）
  - lose: 候选 CI 上界 < production 均值（候选显著更差）
  - tie: CI 与 production 均值重叠（无法判定显著差异）

设计依据：设计文档 D6（加seed+置信区间）+ S11（存完整统计量）。
"""
from __future__ import annotations

import math
from typing import Any

# t 分布临界值（α=0.05 双侧）按自由度查表
_T_CRITICAL = {
    2: 4.303, 4: 2.776, 6: 2.447, 8: 2.306, 10: 2.228,
    18: 2.101, 28: 2.048, 38: 2.024, 48: 2.010,
}


def _t_critical(df: int) -> float:
    """查 t 临界值（α=0.05 双侧），df 不在表里取最近的较大 df（保守）。"""
    if df <= 0:
        return 4.303
    keys = sorted(_T_CRITICAL.keys())
    for k in keys:
        if k >= df:
            return _T_CRITICAL[k]
    return _T_CRITICAL[keys[-1]]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def std(values: list[float]) -> float:
    """样本标准差（除以 N-1）。"""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def confidence_interval(values: list[float]) -> tuple[float, float]:
    """均值的 95% 置信区间。Returns: (ci_low, ci_high)"""
    if len(values) < 2:
        m = mean(values)
        return m, m
    m = mean(values)
    s = std(values)
    n = len(values)
    t = _t_critical(n - 1)
    margin = t * s / math.sqrt(n)
    return m - margin, m + margin


def two_sample_t_test(
    prod_scores: list[float], cand_scores: list[float],
) -> dict[str, Any]:
    """两独立样本 t 检验（Welch's，不假设等方差）。

    prod = baseline，cand = 候选。
    Returns: {mean_prod, std_prod, mean_cand, std_cand, ci_low, ci_high,
              p_value_approx, verdict, confidence}
    """
    mean_prod = mean(prod_scores)
    mean_cand = mean(cand_scores)
    std_prod = std(prod_scores)
    std_cand = std(cand_scores)
    n_p = len(prod_scores)
    n_c = len(cand_scores)

    # 候选均值的置信区间
    ci_low, ci_high = confidence_interval(cand_scores)

    # 两组都无方差：纯比均值
    if std_prod == 0 and std_cand == 0:
        if mean_cand > mean_prod:
            verdict, conf = "win", 1.0
        elif mean_cand < mean_prod:
            verdict, conf = "lose", 1.0
        else:
            verdict, conf = "tie", 0.0
        return {
            "mean_prod": mean_prod, "std_prod": std_prod,
            "mean_cand": mean_cand, "std_cand": std_cand,
            "ci_low": ci_low, "ci_high": ci_high,
            "p_value_approx": 0.0 if verdict != "tie" else 1.0,
            "verdict": verdict, "confidence": conf,
        }

    se = math.sqrt(std_prod**2 / n_p + std_cand**2 / n_c) if (n_p and n_c) else 1.0
    t_stat = (mean_cand - mean_prod) / se if se > 0 else 0.0

    # Welch-Satterthwaite 自由度
    num = (std_prod**2 / n_p + std_cand**2 / n_c) ** 2
    denom_p = (std_prod**2 / n_p) ** 2 / max(n_p - 1, 1)
    denom_c = (std_cand**2 / n_c) ** 2 / max(n_c - 1, 1)
    denom = denom_p + denom_c
    df = num / denom if denom > 0 else (n_p + n_c - 2)

    # 近似 p 值
    t_crit = _t_critical(int(df))
    if abs(t_stat) > t_crit:
        p_approx = 0.04 if abs(t_stat) < t_crit * 1.5 else 0.01
    else:
        p_approx = 0.1 if abs(t_stat) > t_crit * 0.8 else 0.3

    # verdict：候选 CI 与 production 均值是否分离
    if ci_low > mean_prod:
        verdict = "win"
        confidence = min(1.0, (ci_low - mean_prod) / max(se, 0.01))
    elif ci_high < mean_prod:
        verdict = "lose"
        confidence = min(1.0, (mean_prod - ci_high) / max(se, 0.01))
    else:
        verdict = "tie"
        confidence = 0.5

    return {
        "mean_prod": mean_prod, "std_prod": std_prod,
        "mean_cand": mean_cand, "std_cand": std_cand,
        "ci_low": ci_low, "ci_high": ci_high,
        "p_value_approx": p_approx,
        "verdict": verdict, "confidence": confidence,
    }


def decide_verdict(
    prod_scores: list[float], cand_scores: list[float],
) -> dict[str, Any]:
    """A/B verdict 决策入口（封装 two_sample_t_test）。"""
    return two_sample_t_test(prod_scores, cand_scores)
