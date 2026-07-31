"""当前问题卡冻结（REQ-04.8 / DEC-15 / AC-27/28）。

历史检索前，先把每个 finding 冻结成结构化当前问题卡——这是"独立分析"的事实锚点。
后续历史比较只能追加记录，不能覆盖此快照（AC-27）。

卡片内容（REQ-04.8 / AC-27）：
  - 问题陈述（finding.finding）
  - 直接证据（frozen_evidence 片段）
  - 症状（从 finding/evidence 提取的可观察特征）
  - Agent/组件/阶段（发生位置，从分类回钻）
  - 任务场景（分类轴）
  - 影响与严重度（severity）
  - 根因假设及置信度（规则启发式，不用生成式模型）
  - 替代解释（同一症状的其他可能根因）
  - 未知项（证据不足以判断的部分）

问题分组（REQ-04.4 / AC-28）：同根因或同受影响机制的卡归为同一 problem_group。
检索按组顺序注入历史上下文（DEC-16）。

不调用生成式模型（REQ-04.11/AC-39）；根因假设基于规则。
"""
from __future__ import annotations

import logging
from typing import Any

from app.problem_kb import repo
from app.problem_kb.classifier import classify_finding

logger = logging.getLogger("evolution.problem_kb.current_card")

# 根因假设置信度规则（基于 evidence_type）：
# 实证（有 trace 证据）→ 高置信；推断（基于常识）→ 中置信。
_EVIDENCE_TYPE_CONFIDENCE = {
    "实证": 0.8,
    "推断": 0.5,
}


def freeze_current_cards(
    session_id: str,
    eval_dossier: dict[str, Any],
) -> list[dict[str, Any]]:
    """为一次进化的评估卷宗冻结当前问题卡（检索前调用）。

    Args:
        session_id: 进化 session id
        eval_dossier: 评估卷宗（含 findings / frozen_evidence）

    Returns:
        冻结的卡片列表（含 card_id / problem_group / frozen_snapshot），按组顺序。
    """
    cards: list[dict[str, Any]] = []
    try:
        findings = eval_dossier.get("findings") or []
        if not isinstance(findings, list):
            return []
        frozen_evidence = eval_dossier.get("frozen_evidence") or {}
        if isinstance(frozen_evidence, str):
            import json
            frozen_evidence = json.loads(frozen_evidence)

        # 第一遍：逐条构建卡片快照 + 分类
        built: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            fid = str(finding.get("id") or "")
            if not fid:
                continue
            snapshot = _build_snapshot(finding, frozen_evidence)
            classification = classify_finding(finding, frozen_evidence)
            # 查对应问题实例（收录时已建）
            instance = repo.get_instance_by_finding(eval_dossier["dossier_id"], fid)
            built.append({
                "finding": finding,
                "snapshot": snapshot,
                "classification": classification,
                "instance_id": instance["instance_id"] if instance else None,
            })

        # 第二遍：分组（同受影响机制或同 failure_nature 归组，REQ-04.4/AC-28）
        groups = _group_cards(built)

        # 第三遍：写库
        for group_key, members in groups.items():
            for item in members:
                card_id = repo.create_card(
                    session_id=session_id,
                    problem_group=group_key,
                    frozen_snapshot={
                        **item["snapshot"],
                        "classification": item["classification"],
                    },
                    instance_id=item["instance_id"],
                )
                cards.append({
                    "card_id": card_id,
                    "problem_group": group_key,
                    "frozen_snapshot": {
                        **item["snapshot"],
                        "classification": item["classification"],
                    },
                    "instance_id": item["instance_id"],
                })
    except Exception:
        logger.exception("当前问题卡冻结异常 session=%s（已忽略）", session_id)
    return cards


def _build_snapshot(finding: dict[str, Any], frozen_evidence: dict[str, Any]) -> dict[str, Any]:
    """构建单条卡片的结构化快照（REQ-04.8 / AC-27）。"""
    statement = str(finding.get("finding") or "")
    evidence_text = str(finding.get("evidence") or "")
    evidence_type = str(finding.get("evidence_type") or "推断")
    severity = str(finding.get("severity") or "low")
    dimension = str(finding.get("dimension") or "未分类")

    # 直接证据片段：取该 finding 引用的冻结证据
    refs_raw = finding.get("evidence_ref") or finding.get("evidence_id") or []
    if isinstance(refs_raw, str):
        refs = [refs_raw]
    else:
        refs = [str(r) for r in refs_raw]
    direct_evidence: list[dict[str, Any]] = []
    for ref in refs:
        snippet = frozen_evidence.get(ref) or frozen_evidence.get(
            ref[4:] if ref.startswith("evt-") else f"evt-{ref}"
        )
        if isinstance(snippet, dict):
            direct_evidence.append({"evidence_id": ref, "snippet": snippet})

    # 症状：从 statement 提取可观察特征（句首核心描述）
    symptom = statement.split("，")[0].split("。")[0][:120] if statement else ""

    # 根因假设（规则启发式）：基于 dimension + evidence_type
    root_cause, confidence = _infer_root_cause(dimension, evidence_type, evidence_text)

    # 替代解释：同症状的其他可能根因（基于 mechanism 维度）
    alternatives = _infer_alternatives(dimension)

    # 未知项：证据不足的部分
    unknowns: list[str] = []
    if not direct_evidence:
        unknowns.append("无直接 trace 证据片段")
    if evidence_type == "推断":
        unknowns.append("基于推断，缺乏实证")

    return {
        "finding_id": str(finding.get("id") or ""),
        "statement": statement,
        "direct_evidence": direct_evidence,
        "symptom": symptom,
        "severity": severity,
        "dimension": dimension,
        "root_cause_hypothesis": root_cause,
        "root_cause_confidence": confidence,
        "alternative_explanations": alternatives,
        "unknowns": unknowns,
        "evidence_type": evidence_type,
    }


def _infer_root_cause(
    dimension: str, evidence_type: str, evidence_text: str
) -> tuple[str, float]:
    """规则启发式推断根因假设及置信度（不用生成式模型）。

    Returns:
        (根因假设描述, 置信度 0..1)
    """
    confidence = _EVIDENCE_TYPE_CONFIDENCE.get(evidence_type, 0.5)
    # 基于 dimension 给出方向性根因假设
    hypotheses = {
        "协作拓扑": "Agent 间协作拓扑存在结构缺陷（调用链/职责划分/回退路径）",
        "错误保障": "错误处理与保障机制不足（异常未捕获/降级缺失/重试失效）",
        "资源消耗": "资源使用超出预期（token/时间/内存，源于冗余调用或无界循环）",
        "内容质量": "内容生成质量缺陷（源于 prompt/模型能力/上下文不足）",
    }
    root_cause = hypotheses.get(dimension, f"{dimension} 维度存在问题（待进一步定位）")
    return root_cause, confidence


def _infer_alternatives(dimension: str) -> list[str]:
    """同症状的其他可能根因（用于避免锚定，REQ-04 强调独立分析）。"""
    return [
        "可能是偶发性问题（样本不足）",
        "可能是上下游交互导致（非本组件根因）",
    ]


def _group_cards(built: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把卡片按 problem_group 分组（同受影响机制或同 failure_nature，REQ-04.4/AC-28）。

    分组键 = affected_mechanism + "#" + failure_nature。同组说明同根因/同机制，
    检索时整组一起处理（DEC-16）。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in built:
        cls = item["classification"]
        mechanism = cls.get("affected_mechanism") or "未分类"
        nature = cls.get("failure_nature") or "未分类"
        group_key = f"{mechanism}#{nature}"
        groups.setdefault(group_key, []).append(item)
    return groups


__all__ = ["freeze_current_cards"]
