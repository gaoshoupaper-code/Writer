"""规则管理路由：CRUD + 查 trace 命中的规则标红。

第一期：manual 规则增删改查 + 标红查询。
第二期：LLM 候选规则审核（pending/approve/reject）——接口预留。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import app.db as db

router = APIRouter(tags=["rules"])


class Rule(BaseModel):
    id: int | None = None
    name: str
    metric: str            # duration_ms / event_count / total_tokens / error_count / status
    op: str                # > >= < <= == !=
    threshold: str         # 阈值（字符串存，引擎按 metric 类型转换）
    enabled: bool = True
    source: str = "manual"  # manual / llm_candidate
    description: str | None = None


class RuleCreate(BaseModel):
    name: str
    metric: str
    op: str
    threshold: str
    enabled: bool = True
    description: str | None = None


class TraceFlag(BaseModel):
    rule_id: int
    rule_name: str
    metric: str
    op: str
    threshold: str
    metric_value: str


@router.get("/rules", response_model=list[Rule])
def list_rules() -> list[Rule]:
    rows = db.query_all("SELECT * FROM rules ORDER BY id")
    return [
        Rule(
            id=r["id"], name=r["name"], metric=r["metric"], op=r["op"],
            threshold=r["threshold"], enabled=bool(r["enabled"]),
            source=r["source"], description=r["description"],
        )
        for r in rows
    ]


@router.post("/rules", response_model=Rule)
def create_rule(body: RuleCreate) -> Rule:
    now = datetime.now(UTC).isoformat()
    cur = db.execute(
        """INSERT INTO rules (name, metric, op, threshold, enabled, source, created_at, description)
           VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)""",
        (body.name, body.metric, body.op, body.threshold, 1 if body.enabled else 0, now, body.description),
    )
    rid = cur.lastrowid
    return Rule(
        id=rid, name=body.name, metric=body.metric, op=body.op,
        threshold=body.threshold, enabled=body.enabled, source="manual",
        description=body.description,
    )


@router.patch("/rules/{rule_id}", response_model=Rule)
def update_rule(rule_id: int, body: dict[str, Any]) -> Rule:
    """部分更新规则（name/metric/op/threshold/enabled/description）。"""
    existing = db.query_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if existing is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    fields = ["name", "metric", "op", "threshold", "description"]
    sets: list[str] = []
    params: list[Any] = []
    for f in fields:
        if f in body:
            sets.append(f"{f} = ?")
            params.append(body[f])
    if "enabled" in body:
        sets.append("enabled = ?")
        params.append(1 if body["enabled"] else 0)
    if sets:
        params.append(rule_id)
        db.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", tuple(params))
    row = db.query_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    return Rule(
        id=row["id"], name=row["name"], metric=row["metric"], op=row["op"],
        threshold=row["threshold"], enabled=bool(row["enabled"]),
        source=row["source"], description=row["description"],
    )


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int) -> dict[str, str]:
    cur = db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "ok", "deleted": str(rule_id)}


@router.get("/traces/{trace_id}/flags", response_model=list[TraceFlag])
def trace_flags(trace_id: str) -> list[TraceFlag]:
    """查询某 trace 命中的规则标红。"""
    rows = db.query_all(
        """SELECT f.metric_value, r.id, r.name, r.metric, r.op, r.threshold
           FROM trace_flags f JOIN rules r ON r.id = f.rule_id
           WHERE f.trace_id = ? ORDER BY r.id""",
        (trace_id,),
    )
    return [
        TraceFlag(
            rule_id=r["id"], rule_name=r["name"], metric=r["metric"], op=r["op"],
            threshold=r["threshold"], metric_value=r["metric_value"],
        )
        for r in rows
    ]


@router.post("/rules/evaluate/{trace_id}")
def re_evaluate(trace_id: str) -> dict[str, Any]:
    """手动重跑某 trace 的规则评估（改规则后补打标）。"""
    from app.rules_engine import evaluate_trace
    hits = evaluate_trace(trace_id)
    return {"trace_id": trace_id, "hits": hits}


# ── 第二期：LLM-judge + 候选规则审核 ──


@router.post("/judge/{trace_id}")
def trigger_judge(trace_id: str) -> dict[str, Any]:
    """手动触发某 trace 的 LLM-judge 评估（忽略幂等会重评）。

    先删该 trace 的 judgment_runs 记录强制重评。
    """
    db.execute("DELETE FROM judgment_runs WHERE trace_id = ?", (trace_id,))
    db.execute("DELETE FROM trace_scores WHERE trace_id = ?", (trace_id,))
    from app.judge import judge_trace
    from app.llm import judge_enabled
    if not judge_enabled():
        raise HTTPException(status_code=400, detail="LLM 未配置（JUDGE_MODEL/JUDGE_API_KEY）")
    result = judge_trace(trace_id)
    if result is None:
        raise HTTPException(status_code=500, detail="评估失败，见日志")
    return {"trace_id": trace_id, **result}


@router.get("/rules/pending", response_model=list[Rule])
def list_pending_rules() -> list[Rule]:
    """待审核的 LLM 候选规则。"""
    rows = db.query_all("SELECT * FROM rules WHERE source='llm_candidate' AND status='pending' ORDER BY id DESC")
    return [_rule_from_row(r) for r in rows]


@router.post("/rules/{rule_id}/approve")
def approve_rule(rule_id: int) -> dict[str, Any]:
    """批准候选规则：转 enabled manual 规则，后续自动标红。"""
    row = db.query_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.execute("UPDATE rules SET status='approved', enabled=1 WHERE id = ?", (rule_id,))
    # 批准后对所有已有 trace 补打标
    from app.rules_engine import evaluate_trace
    all_traces = db.query_all("SELECT trace_id FROM runs")
    hits = 0
    for t in all_traces:
        hits += evaluate_trace(t["trace_id"])
    return {"status": "approved", "rule_id": rule_id, "new_flags": hits}


@router.post("/rules/{rule_id}/reject")
def reject_rule(rule_id: int) -> dict[str, Any]:
    """拒绝候选规则：标记 rejected（保留记录但永不生效）。"""
    row = db.query_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.execute("UPDATE rules SET status='rejected', enabled=0 WHERE id = ?", (rule_id,))
    return {"status": "rejected", "rule_id": rule_id}


@router.get("/traces/{trace_id}/score")
def trace_score(trace_id: str) -> dict[str, Any]:
    """查询某 trace 的 LLM-judge 评分（最新一条）。"""
    row = db.query_one(
        "SELECT * FROM trace_scores WHERE trace_id = ? ORDER BY id DESC LIMIT 1", (trace_id,)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="无评分记录")
    import json as _json
    return {
        "score": row["score"], "verdict": row["verdict"],
        "rubric": _json.loads(row["rubric_json"]) if row["rubric_json"] else {},
        "summary": row["summary"], "scored_at": row["scored_at"],
    }


def _rule_from_row(r: dict[str, Any]) -> Rule:
    return Rule(
        id=r["id"], name=r["name"], metric=r["metric"], op=r["op"],
        threshold=r["threshold"], enabled=bool(r["enabled"]), source=r["source"],
        description=r.get("description"),
    )
