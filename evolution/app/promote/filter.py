"""规则过滤（数据闭环设计 B2）。

在 judge 之前跑（省 judge 成本），判定 trace 是否有明显质量问题。
任一规则命中 → 过滤（不进 judge，直接标 auto_reject）。

规则（基于 runs 表 + 交付物检查）：
  R1 状态非 completed   — 失败/中断的 trace 没有产出，不能 judge
  R2 无 writing 正文    — 没写出来东西，无法评估内容质量
  R3 正文过短 (<500字)  — 产出量不足，不构成完整作品
  R4 正文过长 (>50000字)— 异常超长，可能死循环
  R5 owner_user_id unknown — 来源不明（D16 应已拦截，兜底）

不做 PII 脱敏（本次明确不做，后续迭代）。
"""
from __future__ import annotations

import logging
from typing import Any

import app.core.db as db
from app.eval_agent import eval_extractor

logger = logging.getLogger("evolution.promote.filter")

# 规则阈值
MIN_CONTENT_LEN = 500       # 正文最少字数
MAX_CONTENT_LEN = 50_000    # 正文最多字数


def check_trace(trace_id: str) -> dict[str, Any]:
    """对一条 trace 跑全部规则，返回过滤结果。

    Returns:
        {
            "passed": bool,           # 是否通过所有规则
            "violations": [str],      # 命中的规则描述
            "content_len": int,       # 正文长度（供 debug）
        }
    """
    violations: list[str] = []

    # R1: trace 状态必须 completed
    run = db.query_one("SELECT status, owner_user_id FROM runs WHERE trace_id=?", (trace_id,))
    if run is None:
        return {"passed": False, "violations": ["trace 不存在"], "content_len": 0}
    if run["status"] != "completed":
        violations.append(f"状态非 completed（当前 {run['status']}）")

    # R5: owner_user_id 不能是 unknown
    if run["owner_user_id"] == "unknown":
        violations.append("owner_user_id 为 unknown（来源不明）")

    # R2-R4: 检查 writing 正文
    content_len = 0
    try:
        content = eval_extractor.get_content_layer_text(trace_id)
        content_len = len(content)
    except Exception:
        # 提取失败（workspace 文件不存在等）视为无正文
        content_len = 0

    if content_len == 0:
        violations.append("无 writing 正文交付物")
    elif content_len < MIN_CONTENT_LEN:
        violations.append(f"正文过短（{content_len} < {MIN_CONTENT_LEN} 字）")
    elif content_len > MAX_CONTENT_LEN:
        violations.append(f"正文过长（{content_len} > {MAX_CONTENT_LEN} 字）")

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "content_len": content_len,
    }


def filter_and_decide(trace_id: str) -> tuple[bool, str | None, dict[str, Any]]:
    """过滤 + 决策：返回 (passed, reject_reason, detail)。

    passed=True → 继续 judge
    passed=False → 跳过 judge，reject_reason 给 set_judge_result 用
    """
    result = check_trace(trace_id)
    if result["passed"]:
        return True, None, result
    return False, "；".join(result["violations"]), result


__all__ = ["check_trace", "filter_and_decide", "MIN_CONTENT_LEN", "MAX_CONTENT_LEN"]
