"""阈值型规则引擎：trace 入库后评估，命中则打标 trace_flags。

规则模型：{name, metric, op, threshold}
- metric ∈ {duration_ms, event_count, total_tokens, error_count, status}
- op ∈ {>, >=, <, <=, ==, !=}
评估时机：trace 摄入完成后（importer 调 evaluate_trace）。
派生指标（total_tokens/error_count）按 trace 实时聚合算出。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import app.core.db as db

# 比较运算符实现
_OPS: dict[str, Any] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _trace_metrics(trace_id: str) -> dict[str, Any]:
    """计算单个 trace 的所有可评估指标。"""
    run = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        return {}
    # error_count：该 trace 的 error 节点数
    err_row = db.query_one(
        "SELECT COUNT(*) AS c FROM nodes WHERE trace_id = ? AND kind = 'error'", (trace_id,)
    )
    # total_tokens：该 trace 的 LLM 节点 token 之和
    tok_row = db.query_one(
        "SELECT COALESCE(SUM(usage_total),0) AS t FROM nodes WHERE trace_id = ? AND kind = 'llm'",
        (trace_id,),
    )
    return {
        "duration_ms": run["duration_ms"],
        "event_count": run["event_count"],
        "total_tokens": int(tok_row["t"]) if tok_row else 0,
        "error_count": int(err_row["c"]) if err_row else 0,
        "status": run["status"],
    }


def _cast_threshold(metric: str, threshold: str) -> Any:
    """阈值按 metric 类型转换。status 是字符串，其余是数值。"""
    if metric == "status":
        return threshold
    try:
        return int(threshold)
    except ValueError:
        return float(threshold)


def evaluate_trace(trace_id: str) -> int:
    """对单个 trace 跑所有启用的规则，命中则打标。返回命中数。

    在 importer 摄入完成后调用。
    """
    metrics = _trace_metrics(trace_id)
    if not metrics:
        return 0

    rules = db.query_all("SELECT id, name, metric, op, threshold FROM rules WHERE enabled = 1")
    hits = 0
    now = datetime.now(UTC).isoformat()
    for rule in rules:
        metric = rule["metric"]
        actual = metrics.get(metric)
        if actual is None:
            continue  # 该指标无值（如 duration_ms 缺失），跳过
        op_fn = _OPS.get(rule["op"])
        if op_fn is None:
            continue
        threshold = _cast_threshold(metric, rule["threshold"])
        try:
            matched = op_fn(actual, threshold)
        except TypeError:
            continue  # 类型不兼容（如数值 op 对字符串），跳过
        if matched:
            db.execute(
                """INSERT OR IGNORE INTO trace_flags (trace_id, rule_id, metric_value, flagged_at)
                   VALUES (?, ?, ?, ?)""",
                (trace_id, rule["id"], str(actual), now),
            )
            hits += 1
    return hits
