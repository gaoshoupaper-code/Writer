"""开发者界面路由：返回 Jinja2 渲染的 HTML 页面。

复用各业务模块的 db 查询（不绕 HTTP），直接渲染数据。
受众：开发者自己（纯内部工具，无登录）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter(tags=["web"], include_in_schema=False)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _all_workspaces() -> list[str]:
    """所有 workspace_id（过滤栏用）。"""
    rows = db.query_all("SELECT DISTINCT workspace_id FROM runs ORDER BY workspace_id")
    return [r["workspace_id"] for r in rows if r["workspace_id"]]


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request) -> HTMLResponse:
    """概览页：数字卡片 + 趋势图 + agent 排行 + 失败模式。"""
    overview = db.query_one(_OVERVIEW_SQL)
    # 预格式化大数字（jinja2 内 format 千分位会与模板语法冲突）
    overview = dict(overview)
    overview["total_tokens_fmt"] = f"{overview['total_tokens']:,}"
    overview["avg_duration_s"] = f"{overview['avg_duration'] / 1000:.1f}"
    return templates.TemplateResponse(
        request, "overview.html",
        {
            "active": "overview",
            "overview": overview,
            "timeline": db.query_all(_TIMELINE_SQL),
            "agents": db.query_all(_AGENT_RANK_SQL),
            "failures": db.query_all(_FAILURE_SQL),
            "durations": [r["duration_ms"] for r in db.query_all(_DURATION_SQL) if r["duration_ms"] is not None],
        },
    )


@router.get("/traces", response_class=HTMLResponse)
def traces_page(
    request: Request,
    workspace: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> HTMLResponse:
    """trace 列表页。"""
    where: list[str] = []
    params: list[Any] = []
    if workspace:
        where.append("r.workspace_id = ?")
        params.append(workspace)
    if status:
        where.append("r.status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.query_all(
        f"""SELECT r.*, (SELECT count(*) FROM trace_flags f WHERE f.trace_id = r.trace_id) AS flag_count
            FROM runs r {where_sql}
            ORDER BY r.started_at DESC LIMIT ? OFFSET ?""",
        tuple(params + [limit, offset]),
    )
    return templates.TemplateResponse(
        request, "traces.html",
        {
            "active": "traces", "traces": rows,
            "workspaces": _all_workspaces(),
            "filter_workspace": workspace or "", "filter_status": status or "",
            "json": json,  # 模板里序列化用
        },
    )


@router.get("/traces/{trace_id}", response_class=HTMLResponse)
def trace_detail_page(request: Request, trace_id: str) -> HTMLResponse:
    """trace 详情页：节点树 + flags。"""
    run = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        return templates.TemplateResponse(request, "empty.html", {"active": "traces", "message": "Trace 不存在"})
    flags = db.query_all(
        """SELECT f.metric_value, r.name, r.metric, r.op, r.threshold
           FROM trace_flags f JOIN rules r ON r.id = f.rule_id
           WHERE f.trace_id = ? ORDER BY r.id""",
        (trace_id,),
    )
    # 重新投影拿节点树（与 /traces/{id} API 一致）
    nodes, events_count = _project_nodes(trace_id, run)
    # LLM-judge 评分（第二期）
    score = db.query_one(
        "SELECT score, verdict, summary, rubric_json FROM trace_scores WHERE trace_id=? ORDER BY id DESC LIMIT 1",
        (trace_id,),
    )
    judgment = db.query_one("SELECT status, error FROM judgment_runs WHERE trace_id=?", (trace_id,))
    import json as _json
    if score and score["rubric_json"]:
        score = dict(score)
        score["rubric"] = _json.loads(score["rubric_json"])
    # prompt 版本：从 run_meta 事件取（后端 recorder 在 trace 收尾时写入）。
    prompt_versions = _extract_prompt_versions(trace_id)
    return templates.TemplateResponse(
        request, "trace_detail.html",
        {
            "active": "traces", "run": run, "nodes": nodes, "flags": flags,
            "events_count": events_count, "score": score, "judgment": judgment,
            "judge_enabled": _judge_enabled(),
            "prompt_versions": prompt_versions,
        },
    )


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request) -> HTMLResponse:
    """规则管理页。"""
    all_rules = db.query_all("SELECT * FROM rules ORDER BY id DESC")
    # 分三组：manual+approved / pending(候选待审) / rejected
    pending = [r for r in all_rules if r["source"] == "llm_candidate" and r.get("status") == "pending"]
    active = [r for r in all_rules if r.get("status") != "pending" and r.get("status") != "rejected"]
    rejected = [r for r in all_rules if r.get("status") == "rejected"]
    hit_counts = {r["rule_id"]: r["c"] for r in db.query_all(
        "SELECT rule_id, count(*) AS c FROM trace_flags GROUP BY rule_id"
    )}
    return templates.TemplateResponse(
        request, "rules.html",
        {
            "active": "rules", "pending": pending, "rules": active, "rejected": rejected,
            "hit_counts": hit_counts, "judge_enabled": _judge_enabled(),
        },
    )


@router.get("/prompts", response_class=HTMLResponse)
def prompts_page(request: Request) -> HTMLResponse:
    """prompt 版本管理页（Phase 4 T9）。"""
    import app.prompts_repo as repo
    prompts = repo.list_prompts()
    # 每个 prompt 附带版本数和当前 production 版本
    enriched = []
    for p in prompts:
        versions = repo.list_versions(p["id"])
        prod = repo.get_version_by_label(p["id"], repo.PRODUCTION_LABEL)
        enriched.append({
            **p,
            "version_count": len(versions),
            "production_version": prod["version"] if prod else None,
            "latest_version": versions[0]["version"] if versions else None,
            "production_content": prod["content"] if prod else None,
        })
    return templates.TemplateResponse(
        request, "prompts.html",
        {"active": "prompts", "prompts": enriched},
    )


@router.get("/active", response_class=HTMLResponse)
def active_page(request: Request) -> HTMLResponse:
    """活跃大盘页（Phase 6 T15）：当前正在运行的 trace。"""
    from app.active import get_active_runs
    runs = get_active_runs()
    return templates.TemplateResponse(
        request, "active.html",
        {"active": "active", "runs": runs},
    )


@router.get("/evaluation", response_class=HTMLResponse)
def evaluation_page(request: Request) -> HTMLResponse:
    """双层评估页（Phase 1 T1.7）：大盘 + 维度均分 + 评估记录列表。"""
    from app.rubrics import xianxia as rubric

    # 大盘
    total = db.query_one("SELECT count(*) AS c FROM evaluation_runs WHERE status='done'")
    badcase_rows = db.query_all(
        """SELECT DISTINCT trace_id FROM evaluation_scores
           WHERE (layer='content' AND score < ?) OR (layer='subagent' AND score < ?)""",
        (rubric.CONTENT_BADCASE_THRESHOLD, rubric.SUBAGENT_BADCASE_THRESHOLD),
    )
    # 各维度均分
    dim_avg = db.query_all(
        """SELECT layer, target, metric, AVG(score) AS avg_score
           FROM evaluation_scores GROUP BY layer, target, metric ORDER BY layer, target"""
    )
    content_avg_rows = [d for d in dim_avg if d["layer"] == "content"]
    subagent_avg_rows = [d for d in dim_avg if d["layer"] == "subagent"]
    content_avg = (sum(d["avg_score"] for d in content_avg_rows) / len(content_avg_rows)) if content_avg_rows else 0
    subagent_avg = (sum(d["avg_score"] for d in subagent_avg_rows) / len(subagent_avg_rows)) if subagent_avg_rows else 0

    # 已评估 trace 列表（每 trace 的双层均分 + badcase 维度数）
    evaluated = db.query_all(
        f"""SELECT er.trace_id, er.status AS eval_status, er.finished_at AS evaluated_at,
            (SELECT AVG(score) FROM evaluation_scores WHERE trace_id=er.trace_id AND layer='content') AS content_avg,
            (SELECT AVG(score) FROM evaluation_scores WHERE trace_id=er.trace_id AND layer='subagent') AS subagent_avg,
            (SELECT count(*) FROM evaluation_scores WHERE trace_id=er.trace_id
             AND ((layer='content' AND score < {rubric.CONTENT_BADCASE_THRESHOLD})
               OR (layer='subagent' AND score < {rubric.SUBAGENT_BADCASE_THRESHOLD}))) AS badcase_count
            FROM evaluation_runs er WHERE er.status='done'
            ORDER BY er.finished_at DESC LIMIT 100"""
    )
    return templates.TemplateResponse(
        request, "evaluation.html",
        {
            "active": "evaluation",
            "overview": {
                "evaluated_count": total["c"] if total else 0,
                "badcase_count": len(badcase_rows),
            },
            "dimension_averages": dim_avg,
            "content_avg": content_avg,
            "subagent_avg": subagent_avg,
            "content_threshold": rubric.CONTENT_BADCASE_THRESHOLD,
            "subagent_threshold": rubric.SUBAGENT_BADCASE_THRESHOLD,
            "evaluated_traces": evaluated,
        },
    )


@router.get("/evaluation/{trace_id}", response_class=HTMLResponse)
def evaluation_detail_page(request: Request, trace_id: str) -> HTMLResponse:
    """单 trace 双层评估详情页：双层分数明细 + badcase + 交付物概要。"""
    from app.rubrics import xianxia as rubric
    from app.eval_extractor import summarize_deliveries

    run = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        return templates.TemplateResponse(request, "empty.html", {"active": "evaluation", "message": "Trace 不存在"})

    eval_run = db.query_one("SELECT * FROM evaluation_runs WHERE trace_id = ?", (trace_id,))
    scores = db.query_all(
        "SELECT layer, target, metric, score, evidence, scored_at "
        "FROM evaluation_scores WHERE trace_id = ? ORDER BY layer, target",
        (trace_id,),
    )
    return templates.TemplateResponse(
        request, "evaluation_detail.html",
        {
            "active": "evaluation", "run": run, "eval_run": eval_run,
            "scores": scores, "deliveries": summarize_deliveries(trace_id),
            "content_threshold": rubric.CONTENT_BADCASE_THRESHOLD,
            "subagent_threshold": rubric.SUBAGENT_BADCASE_THRESHOLD,
            "judge_enabled": _judge_enabled(),
        },
    )


def _judge_enabled() -> bool:
    try:
        from app.llm import judge_enabled
        return judge_enabled()
    except Exception:
        return False


# ── 概览页用的聚合 SQL ──

_OVERVIEW_SQL = """SELECT
    (SELECT count(*) FROM runs) AS total,
    (SELECT count(*) FROM runs WHERE status='completed') AS success,
    (SELECT count(*) FROM runs WHERE status='failed') AS failed,
    (SELECT count(*) FROM runs WHERE status='running') AS running,
    (SELECT count(*) FROM runs WHERE duration_ms IS NOT NULL) AS has_duration,
    COALESCE((SELECT AVG(duration_ms) FROM runs WHERE duration_ms IS NOT NULL), 0) AS avg_duration,
    COALESCE((SELECT SUM(usage_total) FROM nodes WHERE kind='llm'), 0) AS total_tokens"""

_TIMELINE_SQL = """SELECT
    strftime('%Y-%m-%d %H:00', started_at) AS bucket,
    count(*) AS total,
    sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
    FROM runs WHERE started_at IS NOT NULL
    GROUP BY bucket ORDER BY bucket ASC LIMIT 200"""

_AGENT_RANK_SQL = """SELECT
    agent_name,
    count(DISTINCT trace_id) AS call_count,
    count(*) AS node_count,
    COALESCE(AVG(duration_ms), 0) AS avg_duration,
    sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS fail_count
    FROM nodes WHERE kind='agent' AND agent_name IS NOT NULL
    GROUP BY agent_name ORDER BY call_count DESC LIMIT 20"""

_FAILURE_SQL = """SELECT substr(COALESCE(error,'(无错误信息)'), 1, 80) AS pattern,
    count(*) AS cnt, group_concat(trace_id, ',') AS sample_ids
    FROM runs WHERE status='failed' GROUP BY pattern ORDER BY cnt DESC LIMIT 10"""

_DURATION_SQL = "SELECT duration_ms FROM runs WHERE duration_ms IS NOT NULL ORDER BY duration_ms"


def _project_nodes(trace_id: str, run: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """重新投影节点树（供详情页渲染）。返回 (节点列表, 事件数)。"""
    from app import projector
    from app.models import TraceLogEvent, TraceRunSummary
    from app.traces import _reconstruct_incremental_inputs

    event_rows = db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ? ORDER BY sequence", (trace_id,)
    )
    events = [TraceLogEvent.model_validate(json.loads(r["payload_json"])) for r in event_rows]
    # 增量重建（Phase 3 T3.3）：让投影看到完整 input，而非增量碎片。
    events = _reconstruct_incremental_inputs(events)
    summary = TraceRunSummary(
        trace_id=run["trace_id"], workspace_id=run["workspace_id"], thread_id=run["thread_id"] or "",
        session_name=run["session_name"] or "", workspace_path="", endpoint=run["endpoint"] or "",
        status=run["status"], started_at=run["started_at"] or "", ended_at=run["ended_at"],
        duration_ms=run["duration_ms"], event_count=run["event_count"] or 0, path="", error=run["error"],
    )
    projection = projector.TraceProjector().project(summary, events)
    # 按深度缩进排序：run(0) → agent(1) → 叶子
    return [n.model_dump() for n in projection.nodes], len(events)


def _extract_prompt_versions(trace_id: str) -> list[dict[str, Any]]:
    """从 run_meta 事件提取 prompt 版本信息，并按版本号取正文。

    后端 recorder 在 trace 收尾时写一条 type=run_meta 事件，input 含
    {"prompt_versions": {prompt_name: version_int}}。这里查 event_payloads
    取出该字典，再按 name+version 从 prompts 表取正文（旧 trace 可能引用
    已非 production 的历史版本，故按精确 version 取）。

    Returns: [{name, version, found, content, commit_message, created_at}, ...]
             found=False 表示该 prompt/version 在库里已不存在，仍返回 name+version。
    """
    import app.prompts_repo as repo

    rows = db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ? AND type = 'run_meta' ORDER BY sequence",
        (trace_id,),
    )
    versions_map: dict[str, int] = {}
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        pv = (payload.get("input") or {}).get("prompt_versions")
        if isinstance(pv, dict):
            # 合并多个 run_meta（取每个 prompt 最后一次记录的版本）
            versions_map.update({k: int(v) for k, v in pv.items() if isinstance(v, (int, float, str))})

    result: list[dict[str, Any]] = []
    for name, version in versions_map.items():
        try:
            version_int = int(version)
        except (TypeError, ValueError):
            continue
        content_data = repo.get_prompt_version_content(name, version_int)
        if content_data:
            result.append({
                "name": name,
                "version": version_int,
                "found": True,
                "content": content_data["content"],
                "commit_message": content_data.get("commit_message"),
                "created_at": content_data.get("created_at"),
            })
        else:
            result.append({
                "name": name,
                "version": version_int,
                "found": False,
                "content": None,
                "commit_message": None,
                "created_at": None,
            })
    return result
