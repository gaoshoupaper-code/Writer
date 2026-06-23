"""宏观统计路由：4 类指标，实时 GROUP BY。

需求来源：需求文档「宏观面板指标」——错误率趋势/延迟token成本/skill调用排行/失败模式TopN。
第一期只做 token 总量（成本估算需 model 定价表，留框架，先返回 token）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

import app.core.db as db

router = APIRouter(tags=["stats"])


class Overview(BaseModel):
    total: int
    success: int
    failed: int
    error_rate: float          # 0~1
    duration_p50: int | None   # ms
    duration_p90: int | None
    duration_p99: int | None
    total_tokens: int          # 所有 LLM 节点 token 之和
    total_input_tokens: int
    total_output_tokens: int


class SkillStat(BaseModel):
    agent_name: str
    call_count: int            # 该 agent 出现的 trace 数
    node_count: int            # 该 agent 下的节点数（llm/tool）
    avg_duration_ms: int | None
    fail_count: int
    fail_rate: float


class FailurePattern(BaseModel):
    error_pattern: str         # 错误信息归类（前缀）
    count: int
    sample_trace_ids: list[str]


def _percentile(values: list[int], pct: float) -> int | None:
    """简单百分位计算（数据量小，无需近似算法）。"""
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((len(values) - 1) * pct))))
    return values[k]


@router.get("/stats/overview", response_model=Overview)
def overview(
    workspace: str | None = Query(None),
    owner: str | None = Query(None, description="按 owner_user_id 过滤（D16）"),
    hours: int | None = Query(None, description="最近 N 小时，不传则全量"),
) -> Overview:
    """概览：总量、成功/失败、错误率、延迟分位、token 消耗。"""
    where: list[str] = []
    params: list[Any] = []
    if workspace:
        where.append("workspace_id = ?")
        params.append(workspace)
    if owner:
        where.append("owner_user_id = ?")
        params.append(owner)
    if hours:
        where.append("ingested_at >= datetime('now', ?)")
        params.append(f"-{hours} hours")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    runs = db.query_all(
        f"SELECT trace_id, status, duration_ms FROM runs {where_sql}", tuple(params)
    )
    total = len(runs)
    success = sum(1 for r in runs if r["status"] == "completed")
    failed = sum(1 for r in runs if r["status"] == "failed")
    durations = [r["duration_ms"] for r in runs if r["duration_ms"] is not None]

    # token：只统计 LLM 节点（同一 trace 的多 LLM 节点 token 求和）
    token_where = where_sql.replace("workspace_id", "n.workspace_id") if where_sql else ""
    # runs.workspace_id 与 nodes 无直接关联，改用 join
    trace_ids = [r["trace_id"] for r in runs] or ["__none__"]
    placeholders = ",".join("?" * len(trace_ids))
    token_row = db.query_one(
        f"""SELECT
                COALESCE(SUM(usage_input),0) AS ti,
                COALESCE(SUM(usage_output),0) AS to_,
                COALESCE(SUM(usage_total),0) AS tt
            FROM nodes WHERE kind='llm' AND trace_id IN ({placeholders})""",
        tuple(trace_ids),
    )

    return Overview(
        total=total, success=success, failed=failed,
        error_rate=failed / total if total else 0.0,
        duration_p50=_percentile(durations, 0.5),
        duration_p90=_percentile(durations, 0.9),
        duration_p99=_percentile(durations, 0.99),
        total_tokens=token_row["tt"] if token_row else 0,
        total_input_tokens=token_row["ti"] if token_row else 0,
        total_output_tokens=token_row["to_"] if token_row else 0,
    )


@router.get("/stats/skills", response_model=list[SkillStat])
def skill_stats(
    workspace: str | None = Query(None),
    hours: int | None = Query(None),
    top: int = Query(20, ge=1, le=100),
) -> list[SkillStat]:
    """skill（agent_name）调用排行：调用数、平均耗时、失败率。

    按 agent_name 聚合 nodes（kind=agent），call_count = 出现的 trace 数。
    """
    where: list[str] = ["n.kind = 'agent'"]
    params: list[Any] = []
    join_extra = ""
    if workspace:
        join_extra = " JOIN runs r ON r.trace_id = n.trace_id "
        where.append("r.workspace_id = ?")
        params.append(workspace)
    if hours:
        join_extra = " JOIN runs r ON r.trace_id = n.trace_id " if not join_extra else join_extra
        where.append("r.ingested_at >= datetime('now', ?)")
        params.append(f"-{hours} hours")
    where_sql = "WHERE " + " AND ".join(where)

    rows = db.query_all(
        f"""SELECT n.agent_name,
                   COUNT(DISTINCT n.trace_id) AS call_count,
                   COUNT(*) AS node_count,
                   AVG(n.duration_ms) AS avg_dur,
                   SUM(CASE WHEN n.status='failed' THEN 1 ELSE 0 END) AS fail_count
            FROM nodes n {join_extra} {where_sql}
            GROUP BY n.agent_name
            ORDER BY call_count DESC LIMIT ?""",
        tuple(params + [top]),
    )
    result = []
    for r in rows:
        cc = r["call_count"] or 0
        fc = r["fail_count"] or 0
        result.append(SkillStat(
            agent_name=r["agent_name"] or "unknown",
            call_count=cc, node_count=r["node_count"] or 0,
            avg_duration_ms=int(r["avg_dur"]) if r["avg_dur"] is not None else None,
            fail_count=fc, fail_rate=fc / cc if cc else 0.0,
        ))
    return result


class TimelinePoint(BaseModel):
    bucket: str          # 时间桶标签，如 "2026-06-19 14:00"
    total: int
    failed: int


@router.get("/stats/timeline", response_model=list[TimelinePoint])
def timeline(
    workspace: str | None = Query(None),
    hours: int = Query(168, ge=1, le=2160, description="时间范围（小时），默认 7 天"),
    bucket_hours: int = Query(6, ge=1, le=72, description="每桶小时数"),
) -> list[TimelinePoint]:
    """trace 数量随时间变化（错误率趋势图用）。按时间桶聚合。"""
    where = ["started_at IS NOT NULL", "started_at >= datetime('now', ?)"]
    params: list[Any] = [f"-{hours} hours"]
    if workspace:
        where.append("workspace_id = ?")
        params.append(workspace)
    rows = db.query_all(
        f"""SELECT started_at, status FROM runs WHERE {' AND '.join(where)}
            ORDER BY started_at ASC LIMIT 5000""",
        tuple(params),
    )
    if not rows:
        return []
    from datetime import datetime, timedelta

    # 按 bucket_hours 分桶
    buckets: dict[str, dict[str, int]] = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        # 对齐到桶边界
        aligned = dt.replace(minute=0, second=0, microsecond=0)
        aligned = aligned - timedelta(hours=aligned.hour % bucket_hours)
        key = aligned.strftime("%Y-%m-%d %H:%M")
        b = buckets.setdefault(key, {"total": 0, "failed": 0})
        b["total"] += 1
        if r["status"] == "failed":
            b["failed"] += 1
    return [
        TimelinePoint(bucket=k, total=v["total"], failed=v["failed"])
        for k, v in sorted(buckets.items())
    ]


@router.get("/stats/failures", response_model=list[FailurePattern])
def failure_stats(
    workspace: str | None = Query(None),
    hours: int | None = Query(None),
    top: int = Query(10, ge=1, le=50),
) -> list[FailurePattern]:
    """失败模式 TopN：按 error 文本前缀聚类。"""
    where: list[str] = ["status = 'failed'"]
    params: list[Any] = []
    if workspace:
        where.append("workspace_id = ?")
        params.append(workspace)
    if hours:
        where.append("ingested_at >= datetime('now', ?)")
        params.append(f"-{hours} hours")
    where_sql = "WHERE " + " AND ".join(where)

    rows = db.query_all(
        f"""SELECT trace_id, error FROM runs {where_sql} ORDER BY started_at DESC LIMIT 500""",
        tuple(params),
    )
    # 按 error 前 60 字符聚类（运行时聚合，量小无需预聚合）
    buckets: dict[str, list[str]] = {}
    for r in rows:
        err = (r["error"] or "(无错误信息)")[:60]
        buckets.setdefault(err, []).append(r["trace_id"])
    ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    return [
        FailurePattern(error_pattern=err, count=len(ids), sample_trace_ids=ids[:3])
        for err, ids in ranked
    ]
