"""证据编译器：编排提取 → LLM 语义归纳 → 生成索引和角色视图 → 落包。

编译流程：
  1. 前置检查 LLM 配置（未配置则降级为纯规则提取，semantic 层留空）
  2. 提取事实层（extractor.extract_facts）
  3. 检查关键证据（任务契约/产物索引缺失则 failed）
  4. LLM 分段语义归纳（阶段摘要 + 全局重点候选，每条必引证据 ID）
  5. 生成索引层（受控回钻 ID）
  6. 生成角色工作页（投影，不生成新事实）
  7. 落包 + 标记当前版本

成本控制：每包 LLM 调用上限，超额降级为 partial。
状态机：pending → compiling → ready/partial/failed。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import app.core.db as db
import app.core.llm as llm
from app.evidence import repo
from app.evidence.extractor import extract_facts, COMPILE_RULE_VERSION
from app.evidence.prompt import build_stage_summary_prompt, build_global_priorities_prompt

logger = logging.getLogger("evolution.evidence.compiler")

# 每包 LLM 调用上限（4 阶段归纳 + 1 全局分级 + 余量）。超额降级为 partial。
MAX_LLM_CALLS = 20

# 四个阶段名（对应 primary subagent 产物）
_STAGES = ["interview", "storybuilding", "detail-outline", "writing"]


def compile_evidence(trace_id: str, pack_id: str) -> dict[str, Any]:
    """编译一条 trace 的证据包（同步函数，由 API 层 asyncio.to_thread 调用）。

    Returns:
        {"status": "ready"|"partial"|"failed", "reason": str|None, "llm_calls": int}
    """
    llm_calls = 0

    try:
        # 标记 compiling
        repo.update_pack(pack_id, status="compiling")

        # 1. 提取事实层
        facts = extract_facts(trace_id)

        # 2. 检查关键证据
        critical_gap = _check_critical_evidence(facts)
        if critical_gap:
            reason = f"关键证据缺失：{critical_gap}"
            repo.update_pack(
                pack_id, status="failed", failure_reason=reason,
                facts=facts, finished=True,
            )
            return {"status": "failed", "reason": reason, "llm_calls": 0}

        # 3. 生成清单层
        manifest = _build_manifest(trace_id, facts)

        # 4. LLM 语义归纳（或降级）
        llm_ok = llm.judge_enabled()
        if not llm_ok:
            logger.warning("compile_evidence %s: LLM 未配置，语义层降级为空", trace_id)
            semantic = {"stages": [], "priorities": [], "skipped": True,
                        "reason": "LLM 未配置，语义归纳跳过"}
        else:
            try:
                semantic, llm_calls = _compile_semantic_layer(facts)
            except _LLMLimitExceeded as e:
                logger.warning("compile_evidence %s: LLM 调用达上限，降级为 partial", trace_id)
                semantic = e.partial_result
                repo.update_pack(pack_id, status="partial",
                                 failure_reason=f"LLM 调用达上限（{MAX_LLM_CALLS}次）",
                                 llm_calls_used=llm_calls)
            except Exception as exc:
                logger.exception("compile_evidence %s: 语义归纳失败，降级为 partial", trace_id)
                semantic = {"stages": [], "priorities": [], "error": str(exc)}

        # 5. 生成索引层
        index = _build_index_layer(facts, semantic)

        # 6. 生成角色工作页（投影）
        eval_view = _project_eval_view(facts, semantic, index, manifest)
        evolve_view = _project_evolve_view(facts, semantic, index, manifest)

        # 7. 确定最终状态
        has_gaps = bool(facts["coverage"].get("gaps"))
        semantic_skipped = semantic.get("skipped") or semantic.get("error")
        if has_gaps or semantic_skipped:
            final_status = "partial"
            gaps_desc = "; ".join(facts["coverage"].get("gaps", []))
            fail_reason_parts = []
            if has_gaps:
                fail_reason_parts.append(f"证据缺口：{gaps_desc}")
            if semantic_skipped:
                fail_reason_parts.append(f"语义层降级：{semantic.get('reason') or semantic.get('error')}")
            fail_reason = " | ".join(fail_reason_parts)
        else:
            final_status = "ready"
            fail_reason = None

        # 8. 落包
        repo.update_pack(
            pack_id,
            status=final_status,
            manifest=manifest,
            facts=facts,
            semantic=semantic,
            index=index,
            eval_view=eval_view,
            evolve_view=evolve_view,
            failure_reason=fail_reason,
            llm_calls_used=llm_calls,
            finished=True,
        )

        # 9. 标记当前版本（旧包 superseded）
        if final_status in ("ready", "partial"):
            repo.mark_current(pack_id)

        logger.info(
            "compile_evidence %s: 完成 status=%s llm_calls=%d",
            trace_id, final_status, llm_calls,
        )
        return {"status": final_status, "reason": fail_reason, "llm_calls": llm_calls}

    except Exception as exc:
        logger.exception("compile_evidence %s: 编译异常", trace_id)
        repo.update_pack(
            pack_id, status="failed",
            failure_reason=f"编译异常：{exc}",
            finished=True,
        )
        return {"status": "failed", "reason": str(exc), "llm_calls": llm_calls}


# ── 关键证据检查 ──────────────────────────────────────────────


def _check_critical_evidence(facts: dict[str, Any]) -> str | None:
    """检查关键证据是否齐全。缺失则返回原因，齐全返回 None。

    关键证据 = 任务契约可得 + 产物索引（至少知道有哪些产物）。
    缺失这些时评估和进化无法做基本判断。
    """
    contract = facts.get("contract", {})
    if not contract.get("available"):
        return "任务契约不可得（runs 记录不存在）"
    # demand.md 是契约核心，但首期允许缺失（标 partial），不算 critical
    # 真正 critical 的是：连 trace 都跑完了但完全没有产物（ deliveries 空 + review_artifacts 空）
    deliveries = facts.get("deliveries", {})
    review_artifacts = facts.get("review_artifacts", {})
    if not deliveries and not review_artifacts:
        return "无任何产物交付物（primary subagent 和 review subagent 都无 write_file 记录）"
    return None


# ── 清单层 ────────────────────────────────────────────────────


def _build_manifest(trace_id: str, facts: dict[str, Any]) -> dict[str, Any]:
    """生成清单层：契约摘要 + 版本 + 完整度 + 适用维度。"""
    coverage = facts.get("coverage", {})
    contract = facts.get("contract", {})
    run_summary = facts.get("run_summary", {})

    # 确定适用维度（首期简化：有正文产物 → 评估正文；否则只评通用五维）
    has_novel = any(k == "writing" for k in facts.get("deliveries", {}))
    applicable_dims = ["通用五维"]
    if has_novel:
        applicable_dims.append("玄幻正文十一维")

    return {
        "trace_id": trace_id,
        "run_status": run_summary.get("status"),
        "endpoint": run_summary.get("endpoint"),
        "compile_rule_version": COMPILE_RULE_VERSION,
        "provenance": facts.get("provenance", "compile_time_snapshot"),
        "contract_available": contract.get("available", False),
        "contract_demand_md": contract.get("demand_md") is not None,
        "contract_missing": contract.get("missing", []),
        "completeness": "full" if not coverage.get("gaps") else "partial",
        "applicable_dimensions": applicable_dims,
        "coverage": {
            "stage_kinds": coverage.get("stage_kinds", []),
            "agent_count": coverage.get("agent_count", 0),
            "delivery_file_count": coverage.get("delivery_file_count", 0),
            "review_calls": coverage.get("review_calls", 0),
            "revise_inferred": coverage.get("revise_inferred", 0),
            "error_events": coverage.get("error_events", 0),
            "gaps": coverage.get("gaps", []),
        },
    }


# ── LLM 语义层归纳 ───────────────────────────────────────────


class _LLMLimitExceeded(Exception):
    """LLM 调用达上限。携带 partial_result 供降级使用。"""
    def __init__(self, partial_result: dict):
        self.partial_result = partial_result


def _compile_semantic_layer(facts: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """LLM 分段语义归纳。返回 (semantic_dict, llm_call_count)。

    分两步：
      1. 按阶段归纳（interview/storybuilding/detail-outline/writing）
      2. 全局重点候选分级

    每条结论必须引用证据 ID，无引用的被丢弃。
    """
    llm_calls = 0
    stage_summaries: list[dict[str, Any]] = []
    deliveries = facts.get("deliveries", {})
    topology = facts.get("topology", {})
    reliability = facts.get("reliability", {})
    resources = facts.get("resources", {})

    # 按阶段归纳
    for stage in _STAGES:
        if llm_calls >= MAX_LLM_CALLS:
            raise _LLMLimitExceeded({"stages": stage_summaries, "priorities": [],
                                     "reason": f"LLM 调用达上限（{MAX_LLM_CALLS}次），部分阶段未归纳"})

        # 该阶段的产物文本
        artifact_text = _get_stage_artifact_text(stage, deliveries)
        if not artifact_text:
            continue  # 无产物的阶段跳过

        # 该阶段的事件摘要（简化：从 topology/reliability 取该 agent 的指标）
        event_summaries = _get_stage_event_summaries(stage, facts)
        metrics = {
            "total_tokens": _get_stage_tokens(stage, resources),
            "duration_share": _get_stage_duration_share(stage, topology),
            "errors": _get_stage_errors(stage, reliability),
        }

        messages = build_stage_summary_prompt(stage, artifact_text, event_summaries, metrics)
        try:
            raw = llm.chat(messages, temperature=0.0)
            llm_calls += 1
            parsed = _parse_json_response(raw)
            # 校验：丢弃无 evidence_id 的 key_facts
            parsed = _enforce_evidence_refs(parsed)
            stage_summaries.append(parsed)
        except Exception as exc:
            logger.warning("阶段归纳失败 %s: %s", stage, exc)
            stage_summaries.append({"stage": stage, "error": str(exc)})

    # 全局重点候选分级
    priorities = []
    if llm_calls < MAX_LLM_CALLS:
        messages = build_global_priorities_prompt(
            stage_summaries,
            facts.get("recovery_chain", []),
            facts.get("review_chain", []),
            facts.get("revise_events", []),
            reliability,
        )
        try:
            raw = llm.chat(messages, temperature=0.0)
            llm_calls += 1
            parsed = _parse_json_response(raw)
            priorities = _enforce_evidence_refs(parsed).get("priorities", [])
        except Exception as exc:
            logger.warning("全局重点候选分级失败: %s", exc)

    return {
        "stages": stage_summaries,
        "priorities": priorities,
        "llm_calls": llm_calls,
    }, llm_calls


def _get_stage_artifact_text(stage: str, deliveries: dict[str, dict[str, str]]) -> str:
    """取某阶段的产物拼接文本。"""
    files = deliveries.get(stage, {})
    if not files:
        return ""
    parts = [f"## 文件: {path}\n\n{content}" for path, content in sorted(files.items())]
    return "\n\n---\n\n".join(parts)


def _get_stage_event_summaries(stage: str, facts: dict[str, Any]) -> list[dict]:
    """取某阶段的关键事件摘要（简化版，从 review_chain/recovery_chain 提取）。"""
    summaries: list[dict] = []
    # 该阶段的 review 调用
    for rc in facts.get("review_chain", []):
        reviewer = rc.get("reviewer", "")
        if stage in reviewer or _stage_matches_reviewer(stage, reviewer):
            summaries.append({
                "evidence_id": rc.get("evidence_id"),
                "desc": f"review 调用，写入 {rc.get('review_file')}",
            })
    # 该阶段的 revise
    for rv in facts.get("revise_events", []):
        if stage in rv.get("subagent", ""):
            summaries.append({
                "evidence_id": rv.get("evidence_id"),
                "desc": f"修订 {rv.get('revised_path')}（时序推断）",
            })
    return summaries


def _stage_matches_reviewer(stage: str, reviewer: str) -> bool:
    return (
        (stage == "writing" and "writing-review" in reviewer)
        or (stage == "storybuilding" and "storybuilding-review" in reviewer)
        or (stage == "detail-outline" and "detail-outline-review" in reviewer)
    )


def _get_stage_tokens(stage: str, resources: dict) -> int:
    """从 token_share_by_agent 取该阶段的 token（近似）。"""
    share = resources.get("token_share_by_agent", {})
    total = resources.get("total_tokens", 0)
    # agent_name 带 -subagent 后缀
    for agent, ratio in share.items():
        if stage in agent:
            return int(total * ratio)
    return 0


def _get_stage_duration_share(stage: str, topology: dict) -> float:
    share = topology.get("subagent_duration_share", {})
    for agent, ratio in share.items():
        if stage in agent:
            return ratio
    return 0.0


def _get_stage_errors(stage: str, reliability: dict) -> int:
    by_agent = reliability.get("error_by_agent", {})
    return sum(cnt for agent, cnt in by_agent.items() if stage in agent)


# ── 索引层 ────────────────────────────────────────────────────


def _build_index_layer(facts: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    """生成索引层：列出所有可回钻的 node/segment/artifact 标识。

    下游 Agent 只能沿这些 ID 回钻，无 ID 的任意 trace 搜索不属于下游能力。
    """
    # 从事实层收集所有 evidence_id
    evidence_ids: set[str] = set()
    for rc in facts.get("review_chain", []):
        if rc.get("evidence_id"):
            evidence_ids.add(rc["evidence_id"])
    for rv in facts.get("revise_events", []):
        if rv.get("evidence_id"):
            evidence_ids.add(rv["evidence_id"])
    for rc_item in facts.get("recovery_chain", []):
        if rc_item.get("evidence_id"):
            evidence_ids.add(rc_item["evidence_id"])
        if rc_item.get("followed_by", {}).get("evidence_id"):
            evidence_ids.add(rc_item["followed_by"]["evidence_id"])

    # 从语义层收集被引用的 evidence_id
    for stage in semantic.get("stages", []):
        for kf in stage.get("key_facts", []):
            if kf.get("evidence_id"):
                evidence_ids.add(kf["evidence_id"])
    for p in semantic.get("priorities", []):
        if p.get("evidence_id"):
            evidence_ids.add(p["evidence_id"])

    # 产物路径索引
    artifact_paths: list[str] = []
    for files in facts.get("deliveries", {}).values():
        artifact_paths.extend(files.keys())
    artifact_paths.extend(facts.get("review_artifacts", {}).keys())

    return {
        "evidence_ids": sorted(evidence_ids),
        "artifact_paths": sorted(set(artifact_paths)),
        "drill_types": ["event", "artifact", "context_segment"],
        "note": "下游只能沿 evidence_ids 或 artifact_paths 回钻。"
                "evidence_id 格式 evt-{event_id}，可通过 drill 接口加载原始事件。",
    }


# ── 角色工作页投影 ────────────────────────────────────────────


def _project_eval_view(
    facts: dict[str, Any], semantic: dict[str, Any],
    index: dict[str, Any], manifest: dict[str, Any],
) -> dict[str, Any]:
    """投影评估工作页：按评估优先级组织证据阅读顺序。

    评估工作页只组织任务和阅读顺序，不预先生成分数或 finding。
    """
    # 按 P0/P1/P2 分组重点候选
    priorities = semantic.get("priorities", [])
    p0 = [p for p in priorities if p.get("level") == "P0"]
    p1 = [p for p in priorities if p.get("level") == "P1"]
    p2 = [p for p in priorities if p.get("level") == "P2"]

    return {
        "manifest_summary": {
            "run_status": manifest.get("run_status"),
            "completeness": manifest.get("completeness"),
            "applicable_dimensions": manifest.get("applicable_dimensions"),
            "coverage_gaps": manifest.get("coverage", {}).get("gaps", []),
        },
        "priorities": {"P0": p0, "P1": p1, "P2": p2},
        "stage_summaries": semantic.get("stages", []),
        "deliveries_overview": {
            agent: list(files.keys())
            for agent, files in facts.get("deliveries", {}).items()
        },
        "review_chain_summary": [
            {"reviewer": rc.get("reviewer"), "review_file": rc.get("review_file"),
             "evidence_id": rc.get("evidence_id")}
            for rc in facts.get("review_chain", [])
        ],
        "drillable_ids": index.get("evidence_ids", []),
        "instructions": (
            "先按 P0 → P1 → P2 顺序审查重点候选，用 drill_evidence(evidence_id) 回钻原始片段。"
            "证据不足的维度标'无法判断'，不输出臆测分数。"
        ),
    }


def _project_evolve_view(
    facts: dict[str, Any], semantic: dict[str, Any],
    index: dict[str, Any], manifest: dict[str, Any],
) -> dict[str, Any]:
    """投影进化工作页（首期简化版：只做证据归因目录）。

    首期不映射候选 harness 要素，不直接给改进建议。
    完整进化工作页（P0 强制阅读、可复用模式、用户裁决）留第三期。
    """
    return {
        "manifest_summary": {
            "run_status": manifest.get("run_status"),
            "completeness": manifest.get("completeness"),
        },
        "recovery_chain": facts.get("recovery_chain", []),
        "review_chain": facts.get("review_chain", []),
        "revise_events": facts.get("revise_events", []),
        "topology": facts.get("topology", {}),
        "reliability": facts.get("reliability", {}),
        "resources": facts.get("resources", {}),
        "priorities": semantic.get("priorities", []),
        "drillable_ids": index.get("evidence_ids", []),
        "instructions": (
            "进化工作页首期为证据归因目录。结合评估 finding 和这里的过程诊断，"
            "定位哪个阶段/Agent/机制值得改进。不预先映射候选要素。"
        ),
        "note": "首期进化工作页简化版。P0 强制阅读、可复用模式等留第三期。",
    }


# ── 工具函数 ──────────────────────────────────────────────────


def _parse_json_response(raw: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"无法解析 LLM 返回为 JSON: {raw[:200]}")


def _enforce_evidence_refs(parsed: dict[str, Any]) -> dict[str, Any]:
    """丢弃无 evidence_id 的 key_facts 和 priorities（铁律：必引证据）。"""
    if "key_facts" in parsed and isinstance(parsed["key_facts"], list):
        parsed["key_facts"] = [
            kf for kf in parsed["key_facts"]
            if isinstance(kf, dict) and kf.get("evidence_id")
        ]
    if "priorities" in parsed and isinstance(parsed["priorities"], list):
        parsed["priorities"] = [
            p for p in parsed["priorities"]
            if isinstance(p, dict) and p.get("evidence_id")
        ]
    return parsed


__all__ = ["compile_evidence"]
