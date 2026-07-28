"""证据提取器：从一条 trace 提取事实层（FactBundle）。

事实层只记录客观发生过的过程证据，不携带"好/坏"评分，也不携带"应该改什么"的建议。
评分和结论归评估 Agent 和进化 Agent，不归证据编译器。

复用现有原语：
  - load_trace_detail（app.view.traces）→ 完整 trace 投影（nodes/context/events）
  - compute_flow_metrics（app.common.flow_metrics）→ topology/reliability/resources
  - extract_deliveries（app.eval_agent.eval_extractor）→ 4 个 primary subagent 的文件交付物

新写的提取逻辑：
  - review_chain：按 *-review-subagent 重建 review 调用链（flow_metrics 的 review_calls 判据失效）
  - revise_events：时序推断 revise 修改（trace 无显式标记）
  - recovery_chain：error → retry/fallback → 主链继续
  - contract：从 run_start.input 提取任务契约元信息（demand 内容走文件系统补，失败标缺失）
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import app.core.db as db
from app.core.settings import settings
from app.common.flow_metrics import compute_flow_metrics
from app.eval_agent import eval_extractor
from app.view.traces import load_trace_detail

logger = logging.getLogger("evolution.dossier.extractor")

# 冻结交付物正文的单文件字符上限（B1，2026-07-27）。
# 评估 get_content_score 需正文打分；过长章节截断后仍具代表性。
# R22 体积权衡：N 章小说 × 8000 字符在 SQLite TEXT 列内可控。
DELIVERY_FREEZE_CHAR_LIMIT = 8000

# review subagent 命名约定（与包内 subagents/reviewers/ 的 middleware_factory 名对齐）
_REVIEW_AGENT_NAMES = {
    "writing-review-subagent",
    "storybuilding-review-subagent",
    "detail-outline-review-subagent",
}

# review subagent → 它审查的产物路径前缀（用于关联 review 与目标产物）
_REVIEW_TARGET_PATTERNS: dict[str, list[str]] = {
    "writing-review-subagent": ["/chapter/"],
    "storybuilding-review-subagent": ["/character/", "/worldview.md", "/storyline"],
    "detail-outline-review-subagent": ["/detail/"],
}

# primary subagent 名（用于 revise 推断时分组）
_PRIMARY_SUBAGENT_PREFIXES = (
    "interview-subagent",
    "storybuilding-subagent",
    "detail-outline-subagent",
    "writing-subagent",
)

# primary subagent 产出的产物路径前缀（revise 推断用：review 后再写这些路径 = revise）
_PRIMARY_DELIVERY_PREFIXES = (
    "/chapter/", "/detail/", "/storyline", "/character/", "/worldview.md", "/demand.md",
)

# 从 "Updated file /xxx.md" 提取路径的正则（与 eval_extractor 一致）
_PATH_RE = re.compile(r"(?:Updated file|Wrote file)\s+(/[\w\-./]+\.\w+)", re.IGNORECASE)

# compile_rule_version：当前编译规则的版本标识。规则变更时递增。
# 用于判断：同 trace 是否需要重编译（新规则版本 != 旧包的 compile_rule_version）。
COMPILE_RULE_VERSION = "v2"


def extract_facts(trace_id: str) -> dict[str, Any]:
    """从一条 trace 提取事实层。

    Returns:
        FactBundle dict，包含：
        - contract: 任务契约（从 run_start.input + 文件系统补 demand）
        - run_summary: 运行元信息（状态/耗时/事件数/中断原因）
        - topology / reliability / resources: 复用 flow_metrics
        - deliveries: 复用 extract_deliveries（4 个 primary subagent 产物）
        - review_artifacts: review 文件内容（扩展 extract_deliveries 覆盖 review subagent）
        - review_chain: review 调用链（按 *-review-subagent 重建）
        - revise_events: revise 修改（时序推断，confidence=inferred）
        - recovery_chain: error → retry/fallback → 主链继续
        - coverage: 覆盖统计 + 缺口列表

        所有产物标 provenance=compile_time_snapshot（第一期无运行时修订）。
    """
    detail = load_trace_detail(trace_id)
    if detail is None:
        raise ValueError(f"trace 不存在: {trace_id}")

    run = detail.run
    events = detail.events
    nodes = detail.nodes

    # 1. 运行元信息
    run_summary = {
        "trace_id": run.trace_id,
        "status": run.status,
        "endpoint": run.endpoint,
        "duration_ms": run.duration_ms,
        "event_count": run.event_count,
        "error": run.error,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
    }

    # 2. 流程指标（复用 flow_metrics）
    flow_metrics = compute_flow_metrics(detail)

    if run.schema_version >= 2:
        revision_records = _load_v2_artifact_revisions(trace_id, events)
        artifact_revisions = _public_artifact_revisions(revision_records)
        contract = _extract_v2_contract(run, events, revision_records)
        deliveries = _freeze_v2_deliveries(revision_records)
        review_artifacts = _extract_v2_review_artifacts(revision_records)
        provenance = "trace_time"
    else:
        # Legacy traces are only readable for historical inspection. The V2 consumption
        # gate excludes them, so this compatibility path must not invent V2 facts.
        artifact_revisions = _extract_legacy_artifact_revisions(events)
        contract = _extract_legacy_contract(trace_id, run, events)
        deliveries = _freeze_deliveries(trace_id)
        review_artifacts = _extract_review_artifacts(trace_id)
        provenance = "trace_time" if artifact_revisions else "compile_time_snapshot"

    # 6. review 调用链（新写）
    review_chain = _build_review_chain(events)

    # 6.5 review finding 解析（第二期：从 review 文件内容提取结构化 finding + 初查/复查判定）
    review_chain = _enrich_review_chain_with_findings(review_chain, review_artifacts)

    # 7. revise 修改（新写，时序推断）
    revise_events = _infer_revise_events(events)

    # 8. 失败恢复链（新写）
    recovery_chain = _build_recovery_chain(events, nodes)

    # 8.5 memory_quality 埋点（B2）：冻结进 facts，进化侧不再直读 run_meta
    memory_quality = _extract_memory_quality(events)

    # 9. 覆盖统计
    coverage = _compute_coverage(
        contract, flow_metrics, deliveries, review_chain, revise_events,
        recovery_chain, nodes, artifact_revisions,
    )

    return {
        "compile_rule_version": COMPILE_RULE_VERSION,
        "provenance": provenance,
        "run_summary": run_summary,
        "contract": contract,
        "topology": flow_metrics.get("topology", {}),
        "reliability": flow_metrics.get("reliability", {}),
        "resources": flow_metrics.get("resources", {}),
        "deliveries": deliveries,
        "artifact_revisions": artifact_revisions,
        "review_artifacts": review_artifacts,
        "review_chain": review_chain,
        "revise_events": revise_events,
        "recovery_chain": recovery_chain,
        "memory_quality": memory_quality,
        "coverage": coverage,
    }


# ── 交付物冻结（B1，2026-07-27）──────────────────────────────


def _freeze_deliveries(trace_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    """冻结各 primary subagent 的交付物正文（全文截断 + sha256 指纹）。

    复用 eval_extractor.extract_deliveries 的路径定位与 agent 归属逻辑，
    但升级返回结构：每个文件携带冻结正文、字符数、是否截断、内容指纹。
    评估 Agent（阶段 C）可仅凭此冻结正文打分，无需再读工作区文件系统。

    Returns:
        {agent_short_name: {normalized_path: {
            "content_frozen": str,       # 截断后的正文（≤ DELIVERY_FREEZE_CHAR_LIMIT）
            "content_sha256": str,       # 全文（截断前）的 sha256 指纹
            "char_count": int,           # 全文字符数（截断前）
            "truncated": bool,           # 是否因超上限截断
        }, ...}, ...}
        只含实际有可读交付物的 subagent。
    """
    # 复用 eval_extractor 拿到 {agent: {path: content}}（它已读全文，截断 6000）
    # 但我们需要全文 + 自己的截断阈值，故重新读文件系统。
    raw = eval_extractor.extract_deliveries(trace_id)
    if not raw:
        return {}

    frozen: dict[str, dict[str, dict[str, Any]]] = {}
    for agent, files in raw.items():
        agent_frozen: dict[str, dict[str, Any]] = {}
        for path, _truncated_content in files.items():
            # 重新读全文（eval_extractor 返回的是 6000 截断版，这里要全文算指纹）
            full_text = _read_delivery_fulltext(trace_id, path)
            if full_text is None:
                # eval_extractor 读到但这里读不到（并发删除等），保留其截断版作降级
                full_text = _truncated_content
            char_count = len(full_text)
            truncated = char_count > DELIVERY_FREEZE_CHAR_LIMIT
            content_frozen = full_text[:DELIVERY_FREEZE_CHAR_LIMIT] if truncated else full_text
            agent_frozen[path] = {
                "content_frozen": content_frozen,
                "content_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
                "char_count": char_count,
                "truncated": truncated,
            }
        if agent_frozen:
            frozen[agent] = agent_frozen
    return frozen


def _read_delivery_fulltext(trace_id: str, file_path: str) -> str | None:
    """读取交付物的完整正文（不截断）。失败返回 None。

    复用 eval_extractor 的三层路径解析。与 extract_deliveries 的读取一致，
    但返回全文供 _freeze_deliveries 计算指纹和按本模块阈值截断。
    """
    run = db.query_one(
        "SELECT workspace_id, owner_user_id FROM runs WHERE trace_id = ?",
        (trace_id,),
    )
    if run is None:
        return None
    workspace_id = run["workspace_id"]
    owner_user_id = run["owner_user_id"]
    if not owner_user_id or owner_user_id == "unknown":
        return None
    rel = file_path.lstrip("/")
    abs_path = settings.executor_workspace_path / owner_user_id / workspace_id / rel
    try:
        text = abs_path.read_text(encoding="utf-8")
        return text if text.strip() else None
    except (OSError, UnicodeDecodeError):
        return None


# ── memory_quality 提取（B2，2026-07-27）─────────────────────


def _extract_memory_quality(events: list) -> dict[str, Any]:
    """从 run_meta 事件提取 memory_quality 埋点（B2）。

    执行端 memory_recall middleware 每次检索写一条 run_meta 事件，含
    input.memory_quality。进化侧原先直 SQL 读（flow._read_memory_quality_summary），
    现冻结进卷宗 facts，让进化侧切断后仍可读。

    Returns:
        {"entries": [...], "summary": {...}, "available": bool}
        entries 每条含 chapter_num/retrieval_ok/nodes/edges/tokens/error/sequence/evidence_id。
    """
    entries: list[dict[str, Any]] = []
    for evt in events:
        if evt.type != "run_meta":
            continue
        if not isinstance(evt.input, dict):
            continue
        mq = evt.input.get("memory_quality")
        if not mq or not isinstance(mq, dict):
            continue
        entries.append({
            "chapter_num": mq.get("chapter_num"),
            "retrieval_ok": mq.get("retrieval_ok", True),
            "evidence_nodes_count": mq.get("evidence_nodes_count", 0),
            "evidence_edges_count": mq.get("evidence_edges_count", 0),
            "evidence_packet_tokens": mq.get("evidence_packet_tokens", 0),
            "error": mq.get("error"),
            "sequence": evt.sequence,
            "evidence_id": f"evt-{evt.event_id}",
        })

    entries.sort(key=lambda x: x["sequence"])

    if not entries:
        return {"entries": [], "summary": {"available": False}, "available": False}

    ok_count = sum(1 for e in entries if e["retrieval_ok"])
    return {
        "entries": entries,
        "summary": {
            "available": True,
            "total_retrievals": len(entries),
            "ok_count": ok_count,
            "fail_count": len(entries) - ok_count,
            "total_nodes": sum(e["evidence_nodes_count"] for e in entries),
            "total_edges": sum(e["evidence_edges_count"] for e in entries),
            "total_tokens": sum(e["evidence_packet_tokens"] for e in entries),
        },
        "available": True,
    }


# ── 任务契约提取 ──────────────────────────────────────────────


def _extract_v2_contract(
    run: Any, events: list, revisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """优先使用运行时契约快照，缺失 demand 时仅从冻结 revision 补充。"""
    snapshot: dict[str, Any] = {}
    for event in reversed(events):
        if event.type != "run_meta" or not isinstance(event.input, dict):
            continue
        candidate = event.input.get("contract_snapshot")
        if isinstance(candidate, dict):
            snapshot = candidate
            break

    contract: dict[str, Any] = {
        "available": True,
        "provenance": "trace_time",
        "user_goal": snapshot.get("user_goal"),
        "task_type": snapshot.get("task_type") or run.endpoint or run.purpose,
        "promised_artifacts": snapshot.get("promised_artifacts"),
        "hard_constraints": snapshot.get("hard_constraints"),
        "style_preferences": snapshot.get("style_preferences"),
        "scope": snapshot.get("scope"),
        "ambiguities": snapshot.get("ambiguities"),
        "input_references": snapshot.get("input_references"),
        "endpoint": snapshot.get("endpoint") or run.endpoint,
        "thread_id": snapshot.get("thread_id") or run.thread_id,
        "workspace_id": snapshot.get("workspace_id") or run.workspace_id,
        "session_name": snapshot.get("session_name") or run.session_name,
        "run_purpose": snapshot.get("run_purpose") or run.purpose or "user_generation",
        "missing": list(snapshot.get("missing") or []),
    }
    demand_text = snapshot.get("demand_md")
    if not isinstance(demand_text, str) or not demand_text.strip():
        demand_revision = _latest_revision_by_path(revisions).get("/demand.md")
        demand_text = demand_revision.get("_content") if demand_revision else None
    contract["demand_md"] = demand_text[:4000] if isinstance(demand_text, str) else None
    contract["_demand_parsed"] = False
    if contract["demand_md"] is None:
        contract["missing"].append("demand.md 内容（未记录契约快照或不可变 revision）")
    for field in (
        "user_goal", "promised_artifacts", "hard_constraints", "style_preferences",
        "scope", "ambiguities", "input_references",
    ):
        if contract[field] is None:
            contract["missing"].append(f"{field}：V2 trace 未提供")
    return contract


def _extract_legacy_contract(trace_id: str, run: Any, events: list) -> dict[str, Any]:
    """提取任务契约八类信息（首期从 run_start.input + 文件系统补 demand）。

    run_start.input 含 6 字段元信息（endpoint/thread_id/workspace_id/session_name/
    user_id/run_purpose），其余契约字段 trace 不含，走文件系统补 demand.md 或标缺失。
    """
    run_row = db.query_one(
        "SELECT workspace_id, owner_user_id, run_purpose FROM runs WHERE trace_id = ?",
        (trace_id,),
    )
    if run_row is None:
        return {"available": False, "missing": ["runs 记录不存在"]}

    workspace_id = run_row["workspace_id"]
    owner_user_id = run_row["owner_user_id"]
    run_purpose = run_row.get("run_purpose") or "user_generation"

    contract: dict[str, Any] = {
        "available": True,
        "provenance": "compile_time_snapshot",
        # 八类契约字段
        "user_goal": None,
        "task_type": run.endpoint or run_purpose,
        "promised_artifacts": None,
        "hard_constraints": None,
        "style_preferences": None,
        "scope": None,
        "ambiguities": None,
        "input_references": None,
        # 元信息
        "endpoint": run.endpoint,
        "thread_id": None,
        "workspace_id": workspace_id,
        "session_name": None,
        "run_purpose": run_purpose,
        "missing": [],
    }

    # 从 run_start 事件补 thread_id / session_name
    for evt in events:
        if evt.type == "run_start" and isinstance(evt.input, dict):
            inp = evt.input
            contract["thread_id"] = inp.get("thread_id")
            contract["session_name"] = inp.get("session_name")
            break

    # demand.md 内容（契约的核心：用户目标/约束/风格/篇幅都在这里）
    demand_text = _try_read_demand(workspace_id, owner_user_id)
    if demand_text:
        contract["demand_md"] = demand_text[:4000]
        contract["_demand_parsed"] = False  # 拆字段需要 LLM 语义理解，放 compiler 语义层
    else:
        contract["demand_md"] = None
        contract["missing"].append("demand.md 内容（trace 未记录，文件系统读取失败或 owner 缺失）")

    # 标注八类字段哪些缺失（首期都无法从 trace 直接拿到结构化值）
    for field in ("user_goal", "promised_artifacts", "hard_constraints",
                  "style_preferences", "scope", "ambiguities", "input_references"):
        contract["missing"].append(f"{field}：需语义层从 demand.md 提取（首期 trace 不含结构化字段）")

    return contract


def _try_read_demand(workspace_id: str, owner_user_id: str | None) -> str | None:
    """尝试从文件系统读 demand.md（三层路径）。失败返回 None。"""
    if not owner_user_id or owner_user_id == "unknown":
        return None
    try:
        path = settings.executor_workspace_path / owner_user_id / workspace_id / "demand.md"
        text = path.read_text(encoding="utf-8")
        return text if text.strip() else None
    except (OSError, UnicodeDecodeError):
        return None


# ── review 文件内容提取（扩展 extract_deliveries） ────────────


def _extract_review_artifacts(trace_id: str) -> dict[str, str]:
    """提取 review subagent 写的 review/*.md 文件内容。

    与 extract_deliveries 的区别：extract_deliveries 只覆盖 4 个 primary subagent，
    这里专门覆盖 3 个 review subagent 写的 review 文件。

    Returns:
        {normalized_path: content}，只含可读的 review 文件。
    """
    run = db.query_one(
        "SELECT workspace_id, owner_user_id FROM runs WHERE trace_id = ?",
        (trace_id,),
    )
    if run is None:
        return {}
    workspace_id = run["workspace_id"]
    owner_user_id = run["owner_user_id"]
    if not owner_user_id or owner_user_id == "unknown":
        return {}

    # 从 trace 找 review subagent 写的所有 review 文件路径
    rows = db.query_all(
        """SELECT payload_json FROM event_payloads
           WHERE trace_id = ? AND type = 'tool_end'
             AND payload_json LIKE '%write_file%'
           ORDER BY sequence ASC""",
        (trace_id,),
    )

    review_paths: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        agent_name = payload.get("agent_name")
        if agent_name not in _REVIEW_AGENT_NAMES:
            continue
        tool_output = payload.get("tool_output")
        if not isinstance(tool_output, dict):
            continue
        content_text = tool_output.get("content", "")
        if not isinstance(content_text, str):
            continue
        match = _PATH_RE.search(content_text)
        if match:
            review_paths.add(match.group(1))

    # 从文件系统读 review 文件
    result: dict[str, str] = {}
    for path_str in sorted(review_paths):
        rel = path_str.lstrip("/")
        abs_path = settings.executor_workspace_path / owner_user_id / workspace_id / rel
        try:
            text = abs_path.read_text(encoding="utf-8")
            if text.strip():
                result[path_str] = text[:6000]  # 与 deliveries 同样截断
        except (OSError, UnicodeDecodeError):
            logger.debug("读取 review 文件失败 %s", abs_path)

    return result


# ── V2 不可变产物修订 ──────────────────────────────────────────


def _load_v2_artifact_revisions(
    trace_id: str, events: list,
) -> list[dict[str, Any]]:
    """只从已物化的 ArtifactRevision 与受治理 Payload 重建 V2 产物。

    不能用 workspace 补读：workspace 是当前态，无法证明它就是这次运行交付的
    内容。任何 V2 artifact event 缺少物化行、正文或 hash 一致性，都是可信链
    断裂，必须让卷宗编译失败而不是产生被污染的事实。
    """
    rows = db.query_all(
        """SELECT revision.artifact_revision_id, revision.artifact_id,
                  revision.parent_revision_id, revision.payload_id,
                  revision.content_hash, revision.producer_event_id,
                  artifact.logical_key, artifact.artifact_type,
                  payload.content_hash AS payload_content_hash, payload.deleted_at
           FROM artifact_revisions revision
           JOIN artifacts artifact ON artifact.artifact_id=revision.artifact_id
           JOIN payload_objects payload ON payload.payload_id=revision.payload_id
           WHERE revision.producer_trace_id=?""",
        (trace_id,),
    )
    materialized = {row["artifact_revision_id"]: row for row in rows}
    revisions: list[dict[str, Any]] = []

    for event in events:
        if event.type != "artifact_revision":
            continue
        revision_id = event.artifact_revision_id
        artifact = event.artifact
        if not revision_id or not isinstance(artifact, dict):
            raise ValueError(f"V2 artifact event {event.event_id} is missing revision metadata")
        row = materialized.get(revision_id)
        if row is None:
            raise ValueError(f"V2 artifact revision {revision_id} was not materialized")
        payload_ref = event.payload_refs.get("output")
        output = event.output
        content = output.get("content") if isinstance(output, dict) else None
        expected_hash = artifact.get("content_hash")
        if not isinstance(content, str):
            raise ValueError(f"V2 artifact revision {revision_id} has no readable content")
        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            not isinstance(expected_hash, str)
            or actual_hash != expected_hash
            or actual_hash != row["content_hash"]
        ):
            raise ValueError(f"V2 artifact revision {revision_id} content hash mismatch")
        if (
            payload_ref is None
            or row["payload_id"] != payload_ref.payload_id
            or row["payload_content_hash"] != payload_ref.content_hash
            or row["deleted_at"] is not None
        ):
            raise ValueError(f"V2 artifact revision {revision_id} payload reference mismatch")
        if (
            row["producer_event_id"] != event.event_id
            or row["logical_key"] != artifact.get("logical_key")
            or row["artifact_type"] != artifact.get("artifact_type", "workspace_file")
        ):
            raise ValueError(f"V2 artifact revision {revision_id} metadata mismatch")
        revisions.append({
            "artifact_revision_id": revision_id,
            "file_path": row["logical_key"],
            "tool": event.tool_name,
            "fingerprint": actual_hash,
            "agent_name": event.agent_name,
            "sequence": event.sequence,
            "evidence_id": f"evt-{event.event_id}",
            "content_length": len(content),
            "parent_revision_id": row["parent_revision_id"],
            "artifact_type": row["artifact_type"],
            "_content": content,
        })

    revisions.sort(key=lambda item: item["sequence"])
    return revisions


def _public_artifact_revisions(revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep timeline provenance structural; frozen content belongs in deliveries."""
    return [
        {key: value for key, value in revision.items() if key != "_content"}
        for revision in revisions
    ]


def _latest_revision_by_path(revisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for revision in revisions:
        path = revision["file_path"]
        if isinstance(path, str):
            latest[path] = revision
    return latest


def _primary_agent_for_revision(revision: dict[str, Any]) -> str | None:
    agent = eval_extractor._normalize_agent_name(revision.get("agent_name"))
    if agent is not None:
        return agent
    path = revision.get("file_path")
    if not isinstance(path, str):
        return None
    for candidate in eval_extractor.EVALUATION_AGENTS:
        if eval_extractor._path_belongs_to_agent(path, candidate):
            return candidate
    return None


def _freeze_v2_deliveries(revisions: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Freeze only each logical path's final immutable revision for evaluation."""
    frozen: dict[str, dict[str, dict[str, Any]]] = {}
    for path, revision in sorted(_latest_revision_by_path(revisions).items()):
        agent = _primary_agent_for_revision(revision)
        if agent is None:
            continue
        content = revision["_content"]
        char_count = len(content)
        truncated = char_count > DELIVERY_FREEZE_CHAR_LIMIT
        frozen.setdefault(agent, {})[path] = {
            "content_frozen": content[:DELIVERY_FREEZE_CHAR_LIMIT] if truncated else content,
            "content_sha256": revision["fingerprint"],
            "char_count": char_count,
            "truncated": truncated,
            "artifact_revision_id": revision["artifact_revision_id"],
            "sequence": revision["sequence"],
        }
    return frozen


def _extract_v2_review_artifacts(revisions: list[dict[str, Any]]) -> dict[str, str]:
    """Return final review outputs from immutable revisions, never current workspace files."""
    return {
        path: revision["_content"][:6000]
        for path, revision in sorted(_latest_revision_by_path(revisions).items())
        if revision.get("agent_name") in _REVIEW_AGENT_NAMES
    }


# ── Legacy 产物修订快照提取 ────────────────────────────────────


def _extract_legacy_artifact_revisions(events: list) -> list[dict[str, Any]]:
    """从 run_meta 事件的 artifact_snapshot 键提取产物修订时间线。

    第二期执行端在每次 write_file/edit_file 成功后 emit 一条 run_meta 事件，
    含 file_path/tool/fingerprint/content。这里提取为结构化修订记录。

    Returns:
        [{file_path, tool, fingerprint, agent_name, sequence, evidence_id, content_length}, ...]
        按时间排序。无快照事件时返回空列表（provenance 降级为 compile_time_snapshot）。
    """
    revisions: list[dict[str, Any]] = []
    for evt in events:
        if evt.type != "run_meta":
            continue
        if not isinstance(evt.input, dict):
            continue
        snapshot = evt.input.get("artifact_snapshot")
        if not isinstance(snapshot, dict):
            continue
        revisions.append({
            "file_path": snapshot.get("file_path"),
            "tool": snapshot.get("tool"),
            "fingerprint": snapshot.get("fingerprint"),
            "agent_name": snapshot.get("agent_name") or evt.agent_name,
            "sequence": evt.sequence,
            "evidence_id": f"evt-{evt.event_id}",
            "content_length": snapshot.get("content_length"),
        })

    revisions.sort(key=lambda x: x["sequence"])
    return revisions


# ── review 调用链重建 ─────────────────────────────────────────


def _build_review_chain(events: list) -> list[dict[str, Any]]:
    """按 *-review-subagent 重建 review 调用链。

    flow_metrics 的 review_calls 判据（tool_name 含 "review"）失效——review 走的是
    task 工具调用 review subagent，write_file 的 tool_name 是 "write_file" 不含 "review"。
    这里按 agent_name 匹配 *-review-subagent + write_file 事件重建。

    Returns:
        [{reviewer, target_paths, review_file, timestamp, evidence_id, raw_event_id}, ...]
        按时间排序。
    """
    chain: list[dict[str, Any]] = []
    for evt in events:
        if evt.type != "tool_end" or evt.tool_name != "write_file":
            continue
        if evt.agent_name not in _REVIEW_AGENT_NAMES:
            continue
        # 从 tool_output 提取 review 文件路径
        review_file = None
        tool_output = evt.tool_output
        if isinstance(tool_output, dict):
            content = tool_output.get("content", "")
            if isinstance(content, str):
                match = _PATH_RE.search(content)
                if match:
                    review_file = match.group(1)

        # 推断审查目标（按 reviewer 类型映射产物路径前缀）
        target_patterns = _REVIEW_TARGET_PATTERNS.get(evt.agent_name, [])

        chain.append({
            "reviewer": evt.agent_name,
            "review_file": review_file,
            "target_patterns": target_patterns,
            "timestamp": evt.timestamp,
            "sequence": evt.sequence,
            "evidence_id": f"evt-{evt.event_id}",
            "raw_event_id": evt.event_id,
            "status": evt.status,
            "error": evt.error,
        })

    chain.sort(key=lambda x: x["sequence"])
    return chain


# ── review finding 结构化解析（第二期） ────────────────────────

# finding 编号前缀正则：W1/W2/S1/D1 等
_FINDING_ID_RE = re.compile(r"\b([WSD])(\d+)\b")

# 复查状态关键词
_RECHECK_STATUS_RE = re.compile(r"\b(resolved|unresolved|regressed)\b", re.IGNORECASE)


def _enrich_review_chain_with_findings(
    review_chain: list[dict[str, Any]],
    review_artifacts: dict[str, str],
) -> list[dict[str, Any]]:
    """从 review 文件内容提取结构化 finding + 判断初查/复查模式。

    第二期 reviewer prompt 要求每个问题带稳定编号（W1/S1/D1），复查模式逐条确认状态。
    这里从 review 文件内容解析这些信息，附加到 review_chain 的每个条目上。

    对同一 review 文件被写多次的情况（初查写一次、复查覆盖一次），按 review_chain
    条目的时间序判断：同 reviewer 同 review_file 的第一次=初查，第二次=复查。

    Returns:
        在原 review_chain 每条上追加：
        - is_recheck: bool（第二次写同文件 = 复查）
        - findings: [{id, severity, status, note}]（从文件内容解析）
        历史数据（无结构化编号）findings 为空，不报错。
    """
    # 按 reviewer + review_file 统计出现次数，判断初查/复查
    seen_count: dict[tuple[str, str | None], int] = {}

    for entry in review_chain:
        key = (entry["reviewer"], entry.get("review_file"))
        count = seen_count.get(key, 0)
        seen_count[key] = count + 1
        entry["is_recheck"] = count > 0  # 同文件第二次写入 = 复查

        # 从 review_artifacts 拿文件内容解析 finding
        review_file = entry.get("review_file")
        content = review_artifacts.get(review_file, "") if review_file else ""
        entry["findings"] = _parse_findings_from_review(content, entry["is_recheck"])

    return review_chain


def _parse_findings_from_review(content: str, is_recheck: bool) -> list[dict[str, Any]]:
    """从 review 文件内容解析 finding 编号和状态。

    初查模式：提取 W1/W2... 或 S1/S2... 或 D1/D2... 编号。
    复查模式：额外提取每个编号的 resolved/unresolved/regressed 状态。

    历史数据（无编号）返回空列表，不报错。
    """
    if not content:
        return []

    findings: list[dict[str, Any]] = []
    lines = content.split("\n")

    for line in lines:
        # 找含 finding 编号的行
        match = _FINDING_ID_RE.search(line)
        if not match:
            continue

        finding_id = f"{match.group(1)}{match.group(2)}"
        status = "open"  # 初查默认 open

        # 复查模式：找状态关键词
        if is_recheck:
            status_match = _RECHECK_STATUS_RE.search(line)
            if status_match:
                status = status_match.group(1).lower()

        # 避免重复（同编号在同一文件里可能出现多次）
        if not any(f["id"] == finding_id for f in findings):
            findings.append({
                "id": finding_id,
                "status": status,
                "note": line.strip()[:150],  # 保留行内容片段供回钻
            })

    return findings


# ── revise 修改时序推断 ───────────────────────────────────────


def _infer_revise_events(events: list) -> list[dict[str, Any]]:
    """时序推断 revise 修改（trace 无显式标记）。

    算法：
      1. 按 primary subagent 分组事件
      2. 找该 subagent 调用 review 的时刻（task(review-subagent) 的 tool_end）
      3. 该 subagent 在 review 之后对 /chapter/ /detail/ /storyline 等的 write_file → revise

    推断结果标 confidence=inferred（不是确定性证据）。
    第一期没有结构化 finding，无法判断 revise 是否解决了具体问题，只记录"发生过修订"。

    Returns:
        [{subagent, revised_path, after_review_seq, evidence_id, confidence}, ...]
    """
    # 收集每个 primary subagent 的 write_file 事件（可能是初稿或 revise）
    subagent_data: dict[str, dict[str, Any]] = {}

    for evt in events:
        agent = evt.agent_name
        if agent is None:
            continue

        # primary subagent 的 write_file（初稿或 revise 都走这里）
        is_primary = any(agent.startswith(p) for p in _PRIMARY_SUBAGENT_PREFIXES)
        if is_primary and evt.type == "tool_end" and evt.tool_name == "write_file":
            path = None
            tool_output = evt.tool_output
            if isinstance(tool_output, dict):
                content = tool_output.get("content", "")
                if isinstance(content, str):
                    match = _PATH_RE.search(content)
                    if match:
                        path = match.group(1)
            if path and any(path.startswith(p) or p in path for p in _PRIMARY_DELIVERY_PREFIXES):
                d = subagent_data.setdefault(agent, {"writes": []})
                d["writes"].append({"path": path, "sequence": evt.sequence,
                                    "event_id": evt.event_id, "timestamp": evt.timestamp})

    # review 时刻从全局 review-subagent 事件取（比 task 工具的 sanitize 过的 args 可靠）
    review_seqs = sorted(
        evt.sequence for evt in events
        if evt.agent_name in _REVIEW_AGENT_NAMES
    )

    revise_events: list[dict[str, Any]] = []
    for agent, d in subagent_data.items():
        for write in d["writes"]:
            # 找 write 之前最近的 review 时刻
            prior_reviews = [s for s in review_seqs if s < write["sequence"]]
            if not prior_reviews:
                continue  # write 在任何 review 之前 → 初稿，不是 revise
            last_review_seq = prior_reviews[-1]
            revise_events.append({
                "subagent": agent,
                "revised_path": write["path"],
                "after_review_seq": last_review_seq,
                "write_seq": write["sequence"],
                "evidence_id": f"evt-{write['event_id']}",
                "confidence": "inferred",
                "note": "时序推断：review 后同 subagent 再写同路径。"
                        "首期无结构化 finding，无法判断是否解决了具体问题。",
            })

    revise_events.sort(key=lambda x: x["write_seq"])
    return revise_events


# ── 失败恢复链 ────────────────────────────────────────────────


def _build_recovery_chain(events: list, nodes: list) -> list[dict[str, Any]]:
    """重建失败恢复链：error → retry/fallback → 主链是否继续。

    首期只记录客观事件序列（error 事件 + 之后的 tool/llm 事件），
    不做"是否恢复成功"的判断（那需要结合产物达成，留语义层）。

    Returns:
        [{error_event, error_type, agent, sequence, followed_by, recovery_status}, ...]
        recovery_status 首期固定 "unknown"（证据不足，需语义层结合产物判断）。
    """
    chain: list[dict[str, Any]] = []
    error_events = [
        evt for evt in events
        if evt.type.endswith("_error") or evt.type == "run_error"
    ]

    for err in error_events:
        # 找 error 之后的下一个非 error 事件（主链是否继续）
        followed_by = None
        for evt in events:
            if evt.sequence > err.sequence and evt.type in ("tool_start", "llm_start", "run_end"):
                followed_by = {
                    "type": evt.type,
                    "sequence": evt.sequence,
                    "agent_name": evt.agent_name,
                    "evidence_id": f"evt-{evt.event_id}",
                }
                break

        chain.append({
            "error_type": err.type,
            "agent_name": err.agent_name,
            "error_message": (err.error or "")[:300],
            "sequence": err.sequence,
            "evidence_id": f"evt-{err.event_id}",
            "raw_event_id": err.event_id,
            "followed_by": followed_by,
            "recovery_status": "unknown",
            "note": "首期只记录 error 后主链是否继续；'是否恢复成功'需结合产物达成判断。",
        })

    chain.sort(key=lambda x: x["sequence"])
    return chain


# ── 覆盖统计 ──────────────────────────────────────────────────


def _compute_coverage(
    contract: dict[str, Any],
    flow_metrics: dict[str, Any],
    deliveries: dict[str, dict[str, str]],
    review_chain: list[dict[str, Any]],
    revise_events: list[dict[str, Any]],
    recovery_chain: list[dict[str, Any]],
    nodes: list,
    artifact_revisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """计算覆盖统计 + 缺口列表。

    覆盖率不等于质量——它只说明"证据编译器提取到了多少证据"，
    不说明"这次任务做得多好"。
    """
    topology = flow_metrics.get("topology", {})
    reliability = flow_metrics.get("reliability", {})

    stage_kinds = {n.kind for n in nodes}
    agent_names = {n.agent_name for n in nodes if n.agent_name}

    gaps: list[str] = []
    if contract.get("demand_md") is None:
        gaps.append("demand.md 内容缺失，任务契约无法完整重建")
    if not deliveries:
        gaps.append("无 primary subagent 交付物（产物未生成或 owner 缺失）")
    if not review_chain:
        gaps.append("无 review 调用记录（流程未执行 review 或 trace 未记录）")
    # revise 不可证不一定是缺口（可能确实不需要修订），单列说明
    revise_note = "无 revise 推断（可能未修订，或 review 后无同路径再写）" if not revise_events else None

    # provenance：有运行时产物快照 → trace_time；否则 → compile_time_snapshot
    artifact_count = len(artifact_revisions) if artifact_revisions else 0
    provenance = "trace_time" if artifact_count > 0 else "compile_time_snapshot"

    return {
        "stage_kinds": sorted(stage_kinds),
        "agent_count": len(agent_names),
        "agent_names": sorted(agent_names),
        "delivery_agents": list(deliveries.keys()),
        "delivery_file_count": sum(len(v) for v in deliveries.values()),
        "artifact_revisions": artifact_count,
        "review_calls": len(review_chain),
        "revise_inferred": len(revise_events),
        "error_events": reliability.get("error_events_total", 0),
        "tool_calls_total": reliability.get("tool_calls_total", 0),
        "subagent_calls_total": topology.get("subagent_calls_total", 0),
        "gaps": gaps,
        "revise_note": revise_note,
        "provenance": provenance,
    }


__all__ = ["extract_facts", "COMPILE_RULE_VERSION"]
