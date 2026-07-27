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

logger = logging.getLogger("evolution.evidence.extractor")

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
COMPILE_RULE_VERSION = "v1"


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

    # 2. 任务契约（从 run_start.input 提取 + demand.md 走文件系统补）
    contract = _extract_contract(trace_id, run, events)

    # 3. 流程指标（复用 flow_metrics）
    flow_metrics = compute_flow_metrics(detail)

    # 3.5 产物修订快照（第二期：从 run_meta 的 artifact_snapshot 键提取）
    artifact_revisions = _extract_artifact_revisions(events)

    # 4. 产物交付物（复用 extract_deliveries）
    deliveries = eval_extractor.extract_deliveries(trace_id)

    # 5. review 文件内容（扩展：覆盖 review subagent 写的 review/*.md）
    review_artifacts = _extract_review_artifacts(trace_id)

    # 6. review 调用链（新写）
    review_chain = _build_review_chain(events)

    # 6.5 review finding 解析（第二期：从 review 文件内容提取结构化 finding + 初查/复查判定）
    review_chain = _enrich_review_chain_with_findings(review_chain, review_artifacts)

    # 7. revise 修改（新写，时序推断）
    revise_events = _infer_revise_events(events)

    # 8. 失败恢复链（新写）
    recovery_chain = _build_recovery_chain(events, nodes)

    # 9. provenance 判定：有运行时产物快照 → trace_time；否则 → compile_time_snapshot
    provenance = "trace_time" if artifact_revisions else "compile_time_snapshot"

    # 10. 覆盖统计
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
        "coverage": coverage,
    }


# ── 任务契约提取 ──────────────────────────────────────────────


def _extract_contract(trace_id: str, run: Any, events: list) -> dict[str, Any]:
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


# ── 产物修订快照提取（第二期） ────────────────────────────────


def _extract_artifact_revisions(events: list) -> list[dict[str, Any]]:
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
