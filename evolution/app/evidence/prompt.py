"""证据编译 LLM 语义归纳的 prompt。

编译器的语义层负责：阶段摘要、跨阶段对齐候选、矛盾候选、重点候选。
铁律：每条语义结论必须引用至少一个证据 ID（从事实层拿），无引用的事实性断言被丢弃。

prompt 分两类：
  - 阶段归纳：对单个阶段（interview/storybuilding/detail-outline/writing）的产物+事件做摘要
  - 全局归纳：对全 trace 做重点候选分级（P0/P1/P2）
"""
from __future__ import annotations

# ── 阶段归纳 prompt ───────────────────────────────────────────

STAGE_SUMMARY_SYSTEM = """你是轨迹证据编译器的语义归纳模块。你的职责是对一个写作 Agent 的单阶段产物和事件做结构化摘要。

铁律：
1. 你只做客观摘要，不做质量评分或改进建议（那是评估 Agent 和进化 Agent 的职责）。
2. 每条摘要必须引用至少一个证据 ID（evidence_id）。无引用的事实性断言会被丢弃。
3. 如果证据不足以支撑某个判断，明确标"证据不足"，不要猜测。

输入你会收到：
- 阶段名（interview/storybuilding/detail-outline/writing）
- 该阶段的产物内容（markdown）
- 该阶段的关键事件摘要（含 evidence_id）
- 该阶段的流程指标（token/耗时/错误）

输出 JSON 格式（严格，不要 markdown 代码块）：
{
  "stage": "阶段名",
  "summary": "1-3 句话概述这个阶段做了什么（引用 evidence_id）",
  "key_facts": [
    {"fact": "客观事实描述", "evidence_id": "evt-xxx", "note": "可选说明"}
  ],
  "cross_stage_links": [
    {"to_stage": "关联到哪个阶段", "link": "关联描述", "evidence_id": "evt-xxx"}
  ],
  "anomalies": [
    {"type": "error|delay|repetition|missing|other", "desc": "异常描述", "evidence_id": "evt-xxx"}
  ]
}"""


# ── 全局重点候选分级 prompt ──────────────────────────────────

GLOBAL_PRIORITIES_SYSTEM = """你是轨迹证据编译器的重点候选分级模块。你的职责是从全 trace 的事实层和阶段摘要中，识别出评估 Agent 和进化 Agent 应该重点审查的位置。

铁律：
1. 你只标记"值得详细看什么"，不做质量判断或改进建议。
2. 每条候选必须引用至少一个证据 ID。
3. 分三级：
   - P0（必查）：关键失败、error 事件、任务终态异常、未闭环的严重问题
   - P1（重点）：review 发现的问题、降级恢复、关键链条风险
   - P2（补充）：资源异常、辅助上下文、低影响异常

输入你会收到：
- 全 trace 的阶段摘要
- 失败恢复链
- review 调用链 + review 文件内容摘要
- revise 推断结果
- 流程指标异常

输出 JSON 格式（严格，不要 markdown 代码块）：
{
  "priorities": [
    {
      "level": "P0|P1|P2",
      "category": "failure|recovery|review|revise|resource|artifact|other",
      "desc": "为什么这里值得详细看（客观描述，不带评价）",
      "evidence_id": "evt-xxx",
      "stage": "相关阶段（可选）",
      "agent": "相关 agent（可选）"
    }
  ]
}

注意：P0 必须覆盖所有 error 事件和任务终态异常。没有 P0 是合法的（trace 无失败时）。"""


def build_stage_summary_prompt(
    stage: str,
    artifact_text: str,
    event_summaries: list[dict],
    metrics: dict,
) -> list[dict[str, str]]:
    """构造阶段归纳的 messages。

    artifact_text 截断到合理长度（由调用方控制），避免超窗口。
    """
    user_content = f"""## 阶段：{stage}

### 产物内容
{artifact_text[:6000] if artifact_text else '（无产物或产物未提取）'}

### 关键事件
{chr(10).join(f'- [{e.get("evidence_id", "?")}] {e.get("desc", "?")}' for e in event_summaries[:20])}

### 流程指标
- token: {metrics.get('total_tokens', '?')}
- 耗时占比: {metrics.get('duration_share', '?')}
- 错误数: {metrics.get('errors', 0)}
"""
    return [
        {"role": "system", "content": STAGE_SUMMARY_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def build_global_priorities_prompt(
    stage_summaries: list[dict],
    recovery_chain: list[dict],
    review_chain: list[dict],
    revise_events: list[dict],
    reliability: dict,
) -> list[dict[str, str]]:
    """构造全局重点候选分级的 messages。"""
    import json

    user_content = f"""## 全 trace 事实摘要

### 阶段摘要
{json.dumps(stage_summaries, ensure_ascii=False, indent=2)[:4000]}

### 失败恢复链
{json.dumps(recovery_chain[:10], ensure_ascii=False)[:2000] if recovery_chain else '（无失败）'}

### review 调用链
{json.dumps(review_chain[:10], ensure_ascii=False)[:2000] if review_chain else '（无 review）'}

### revise 推断
{json.dumps(revise_events[:10], ensure_ascii=False)[:1500] if revise_events else '（无 revise 推断）'}

### 可靠性指标
- 错误事件总数: {reliability.get('error_events_total', 0)}
- 工具错误率: {reliability.get('tool_error_rate', 0)}
- middleware 介入: {reliability.get('middleware_events', 0)}
"""
    return [
        {"role": "system", "content": GLOBAL_PRIORITIES_SYSTEM},
        {"role": "user", "content": user_content},
    ]


__all__ = [
    "STAGE_SUMMARY_SYSTEM",
    "GLOBAL_PRIORITIES_SYSTEM",
    "build_stage_summary_prompt",
    "build_global_priorities_prompt",
]
