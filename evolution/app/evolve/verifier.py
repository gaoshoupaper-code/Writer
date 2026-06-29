"""verifier —— 多次打分均值（从 adapt 迁移，评估判据 D4/R5）。

封装 evaluation.py 的双层评估，跑 J 次 LLM-judge 取均值降方差。
作为进化的固定 verifier（跨 harness 版本 reward 可比）。

策略：
  - 绕过 evaluation.evaluate_trace 的幂等（它有 done 就跳过）
  - 直接调底层 _evaluate_content_layer J 次
  - 取每次的 overall 分数，J 次取均值

注意：J 次打分有 J 倍 judge 成本（evaluation 一次 5 调用，J=3 即 15 次/trace）。
"""
from __future__ import annotations

import logging
import statistics
from typing import Any

from app.core import llm
from app.diagnosis import eval_extractor

logger = logging.getLogger("evolution.evolve.verifier")

# 默认多次打分次数
DEFAULT_J = 3


def score_trace(trace_id: str, j: int = DEFAULT_J) -> dict[str, Any]:
    """对一个 trace 跑 J 次评估取均值（降方差）。

    Args:
        trace_id: 要评估的 trace
        j:        打分次数（默认 3）

    Returns:
        {
            "overall": float,          # J 次 overall 均值（0-1）
            "std": float,              # J 次 overall 标准差（方差指标）
            "samples": list[float],    # 每次的 overall 分数
            "skipped": bool,           # 是否因无交付物跳过
        }
    """
    if not llm.judge_enabled():
        logger.warning("verifier 跳过：LLM judge 未配置")
        return {"overall": 0.0, "std": 0.0, "samples": [], "skipped": True}

    # 提取交付物（只提一次，J 次评估共用）
    deliveries = eval_extractor.extract_deliveries(trace_id)
    content_text = eval_extractor.get_content_layer_text(trace_id)
    if not content_text:
        logger.warning("verifier 跳过 %s：无 writing 正文", trace_id)
        return {"overall": 0.0, "std": 0.0, "samples": [], "skipped": True}

    # 延迟 import（evaluation 模块可能间接依赖 db 操作）
    from app.diagnosis.evaluation import _evaluate_content_layer

    # 跑 J 次 content 层评估，取 overall
    samples: list[float] = []
    for i in range(j):
        try:
            result = _evaluate_content_layer(trace_id, deliveries)
            if not result.get("skipped"):
                overall = float(result.get("overall", 0))
                samples.append(overall)
        except Exception:
            logger.warning("verifier 第 %d/%d 次打分失败 %s", i + 1, j, trace_id, exc_info=True)

    if not samples:
        logger.warning("verifier %s：J 次打分全部失败", trace_id)
        return {"overall": 0.0, "std": 0.0, "samples": [], "skipped": True}

    mean_overall = statistics.mean(samples)
    std_overall = statistics.stdev(samples) if len(samples) > 1 else 0.0

    logger.info(
        "verifier %s：%d 次打分 overall=%.3f±%.3f samples=%s",
        trace_id, len(samples), mean_overall, std_overall,
        [round(s, 3) for s in samples],
    )
    return {
        "overall": mean_overall,
        "std": std_overall,
        "samples": samples,
        "skipped": False,
    }


__all__ = ["DEFAULT_J", "score_trace"]
