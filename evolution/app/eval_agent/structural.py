"""结构性维度 finding 生成器（FR-002，确定性）。

消费证据卷宗 manifest 的 contract_coverage_matrix（FR-001 确定性求值结果），
把其中 status==missing 的 structural_omission 项转成评估 finding。
这是"缺失性缺陷"检测的 finding 产出层——即使内容维度（LLM 28 维）全绿，
只要结构性契约有违反，就生成显式 finding（DEC-008 冲突仲裁：契约优先）。

确定性铁律（CON-003）：本模块 0 LLM 调用，纯 dict 加工。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("evolution.eval_agent.structural")

# MASFT omission 命名词汇表（DEC-002）：命名词借 MASFT，实际检测能力是
# "契约覆盖到的 omission"。每条 finding 的 direction 字段（FR-010）给"该往哪改"
# 的方向提示（非具体方案，且不含"改评估器/契约"——CON-002 不破）。
_MASFT_DIRECTION = {
    "memory_recalled": (
        "MASFT FM-2.4 信息隐匿：记忆系统本该参与但 trace 中无成功召回。"
        "检查记忆召回中间件是否被装配、记忆 backend 是否健康、是否在写作 subagent 的"
        "middleware 链里。"
    ),
    "subagents_complete": (
        "MASFT 必要协作缺失：应参与的 subagent 没出现。"
        "检查 harness __init__ 是否装配了所有约定的 subagent、subagent 派发条件是否漏了某场景。"
    ),
    "review_executed": (
        "MASFT FM-3.2 未验证：契约要求 review 但 trace 中无 review 记录。"
        "检查 review subagent 是否被装配、review 触发条件是否满足、review 链是否被短路。"
    ),
}


def structural_violation_id(key: str) -> str:
    """结构契约违反 ID（稳定，按 key 派生，不依赖顺序）。

    进化端 evidence_ref 校验（FR-004 多源）按此 ID 识别契约违反证据源。
    格式：cv-<key>，如 cv-memory_recalled。
    """
    return f"cv-{key}"


def build_structural_findings(
    contract_matrix: dict[str, Any] | None,
    facts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """从契约覆盖矩阵产出结构性 finding（FR-002 确定性层）。

    Args:
        contract_matrix: 证据卷宗 manifest.contract_coverage_matrix（FR-001 求值结果）。
            None / 空 → 返回空（契约未跑，不强行产出）。
        facts: 证据卷宗 facts（用于补充 evidence 描述）。可选。

    Returns:
        结构性 finding dict 列表，每条含：
          dimension="结构性"、severity="high"、evidence_type="实证"、
          finding / evidence / evidence_ref=[cv-<key>]、direction / source_class=sealed。
        无违反时返回空列表（结构性维度全绿，正常）。

    确定性：纯 dict 加工，0 LLM 调用（CON-003）。
    """
    if not contract_matrix:
        return []

    findings: list[dict[str, Any]] = []
    for item in contract_matrix.get("items") or []:
        if item.get("dim") != "structural_omission":
            continue
        if item.get("status") != "missing":
            # na（契约未声明）与 covered（满足期望）都不产出 finding。
            continue
        key = item.get("key", "unknown")
        reason = item.get("reason") or "结构性契约违反"
        evidence_payload = item.get("evidence")
        cv_id = structural_violation_id(key)
        direction = _MASFT_DIRECTION.get(key)
        findings.append({
            "dimension": "结构性",
            # 缺失性缺陷一律 high（DEC-008 仲裁：契约命中违反即生成 finding，
            # 不被内容维度全绿覆盖；severity high 反映"结构性问题比内容瑕疵优先级高"）。
            "severity": "high",
            "evidence_type": "实证",
            "finding": reason,
            "evidence": (
                f"契约违反 {cv_id}：{reason}（实际值={evidence_payload}）"
                if evidence_payload is not None else
                f"契约违反 {cv_id}：{reason}"
            ),
            # 关键：evidence_ref 引用契约违反 ID，满足 sealer._validate_completeness
            # 对 finding 必带证据引用的要求，同时让进化端 evidence_ref 多源校验（FR-004）识别。
            "evidence_ref": [cv_id],
            # FR-010 direction（方向非方案；不准建议改评估器/契约，CON-002 不破）
            "direction": direction,
            # ASM-004 归因链分级：契约违反是 sealed（确定性、强可审计、不衰减）
            "source_class": "sealed",
        })

    if findings:
        logger.info(
            "结构性维度产出 %d 条 finding（缺失性缺陷，DEC-008 契约优先）",
            len(findings),
        )
    return findings


__all__ = ["build_structural_findings", "structural_violation_id"]
