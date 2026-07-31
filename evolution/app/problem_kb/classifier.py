"""多轴分类提取器（REQ-02 / DEC-19 / AC-32）。

从评估 finding + 冻结证据片段提取四轴分类：
  - location（发生位置）：从 frozen_evidence 的 agent_name / type / sequence 回钻 trace
    真实结构（REQ-02.1 真实结构锚定）。提取不到记 UNCLASSIFIED。
  - affected_mechanism（受影响机制）：直接取 finding.dimension（已受控）。
  - failure_nature（失败性质）：按 finding/evidence 文本关键词规则映射到受控词表。
  - task_scenario（任务场景）：按 evidence 文本关键词规则映射。

设计原则（REQ-02.4 / AC-32）：
  - 不调用生成式模型，纯规则。
  - 无法归类一律 UNCLASSIFIED，保留 raw_description。
  - 不静默创造类别。
"""
from __future__ import annotations

from typing import Any

from app.problem_kb.taxonomy import (
    UNCLASSIFIED,
    validate_axis,
)

# ── 关键词 → 受控值 规则表（按命中优先级排列）────────────────
# 第一条命中的规则胜出；都不命中 → UNCLASSIFIED。
# 这些关键词基于 finding.statement / finding.evidence 的常见表述。

_FAILURE_NATURE_RULES: list[tuple[str, str]] = [
    ("超时", "超时"),
    ("timeout", "超时"),
    ("卡死", "超时"),
    ("hang", "超时"),
    ("异常", "错误"),
    ("错误", "错误"),
    ("error", "错误"),
    ("失败", "错误"),
    ("抛出", "错误"),
    ("崩溃", "错误"),
    ("退化", "退化"),
    ("回退", "退化"),
    ("下降", "退化"),
    ("变差", "退化"),
    ("丢失", "缺失"),
    ("遗漏", "缺失"),
    ("缺失", "缺失"),
    ("没有", "缺失"),
    ("缺失", "缺失"),
    ("未包含", "缺失"),
    ("重复", "冗余"),
    ("啰嗦", "冗余"),
    ("冗余", "冗余"),
    ("幻觉", "冗余"),
    ("多余", "冗余"),
]

_TASK_SCENARIO_RULES: list[tuple[str, str]] = [
    ("长篇", "长篇"),
    ("连载", "长篇"),
    ("全本", "长篇"),
    ("短篇", "短篇"),
    ("单章", "短篇"),
    ("短文", "短篇"),
    ("续写", "续写"),
    ("接着", "续写"),
    ("继续", "续写"),
    ("重写", "重写"),
    ("修订", "重写"),
    ("修改", "重写"),
    ("润色", "重写"),
]


def _match_rules(text: str, rules: list[tuple[str, str]]) -> str:
    """按规则表顺序匹配，返回首个命中的受控值；不命中返回 UNCLASSIFIED。"""
    if not text:
        return UNCLASSIFIED
    lowered = text.lower()
    for keyword, value in rules:
        if keyword in lowered:
            return value
    return UNCLASSIFIED


def _extract_location(frozen_evidence: dict[str, Any], refs: list[str]) -> dict[str, str]:
    """从冻结证据片段回钻发生位置（REQ-02.1 真实结构锚定 / AC-32）。

    冻结证据片段结构（见 sealer.collect_frozen_evidence）：{evidence_id: {
        type, agent_name, sequence, error?, tool_output?, output?
    }}。取该 finding 引用的第一条有效片段的结构字段。

    Returns:
        {agent, component, stage} —— agent=agent_name，component=type，stage=sequence。
        任一字段提取不到记 UNCLASSIFIED。
    """
    location = {"agent": UNCLASSIFIED, "component": UNCLASSIFIED, "stage": UNCLASSIFIED}
    if not frozen_evidence or not refs:
        return location
    for ref in refs:
        # 冻结证据 key 可能带 evt- 前缀，也可能不带；都试一次
        snippet = frozen_evidence.get(ref) or frozen_evidence.get(
            ref[4:] if ref.startswith("evt-") else f"evt-{ref}"
        )
        if not isinstance(snippet, dict):
            continue
        agent = snippet.get("agent_name")
        component = snippet.get("type")
        sequence = snippet.get("sequence")
        if agent:
            location["agent"] = str(agent)
        if component:
            location["component"] = str(component)
        if sequence is not None:
            location["stage"] = str(sequence)
        if agent or component or sequence is not None:
            break  # 第一条有效片段即可代表发生位置
    return location


def classify_finding(
    finding: dict[str, Any],
    frozen_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对一个 finding 提取多轴分类（REQ-02 / AC-32）。

    Args:
        finding: 评估 finding（含 id/dimension/severity/finding/evidence/evidence_ref）
        frozen_evidence: 该评估卷宗冻结的证据片段 {evidence_id: 片段}

    Returns:
        {
            "location": {agent, component, stage},      # 真实结构回钻
            "affected_mechanism": str,                  # 受控（来自 dimension）
            "failure_nature": str,                      # 受控（规则映射）
            "task_scenario": str,                       # 受控（规则映射）
        }
    """
    frozen_evidence = frozen_evidence or {}

    # 受影响机制：直接取 finding.dimension（已受 eval_agent prompt 约束为四类）
    mechanism = validate_axis("affected_mechanism", str(finding.get("dimension") or ""))

    # 发生位置：回钻冻结证据片段
    refs_raw = finding.get("evidence_ref") or finding.get("evidence_id") or []
    if isinstance(refs_raw, str):
        refs = [refs_raw]
    else:
        refs = [str(r) for r in refs_raw]
    location = _extract_location(frozen_evidence, refs)

    # 失败性质 / 任务场景：规则映射 finding.statement + evidence 文本
    statement = str(finding.get("finding") or "")
    evidence_text = str(finding.get("evidence") or "")
    # 拼接语句 + 证据文本一起喂规则，提高命中率
    combined = f"{statement} {evidence_text}"
    failure_nature = validate_axis("failure_nature", _match_rules(combined, _FAILURE_NATURE_RULES))
    task_scenario = validate_axis("task_scenario", _match_rules(combined, _TASK_SCENARIO_RULES))

    return {
        "location": location,
        "affected_mechanism": mechanism,
        "failure_nature": failure_nature,
        "task_scenario": task_scenario,
    }


__all__ = ["classify_finding"]
