"""evolver 节点 —— 读 landscape → 产 K 个候选 edits（Phase 8，Task 7.4）。

论文 §4.3：每个候选 = typed builder operation + change manifest。
产出 compose 的 edit 指令（{op, target, spec, manifest}），apply 到 baseline config 得候选 config。

revision 模式（E7a）：读 critic 的 revision_feedback，修订指定候选。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.adapt.state import AdaptState, Candidate
from app.compose import config as cfg
from app.compose import edits as edit_ops
from app.core import llm

logger = logging.getLogger("evolution.adapt.evolver")

# 轻档 K=1-2（决策 A4）
DEFAULT_K = 2


def evolver(state: AdaptState) -> dict:
    """读 landscape → 产 K 个候选（含 edits + manifest + config）。

    Returns: {candidates: list[Candidate], revision_count: 更新}
    """
    if not llm.judge_enabled():
        logger.warning("evolver 跳过：LLM 未配置")
        return {"candidates": []}

    revision_target = state.get("revision_target", -1)
    revision_feedback = state.get("revision_feedback", "")

    # revision 模式：修订指定候选
    if revision_target >= 0 and revision_feedback:
        return _handle_revision(state, revision_target, revision_feedback)

    # 正常模式：产 K 个新候选
    return _generate_candidates(state)


def _generate_candidates(state: AdaptState) -> dict:
    """正常模式：读 landscape → LLM 产 K 个候选 edits。"""
    landscape = state.get("landscape", "")
    baseline_config = state["baseline_config"]

    # LLM 产出候选 edit 指令（JSON）
    raw_candidates = _llm_generate_edits(landscape, baseline_config, state.get("round", 0))

    candidates: list[Candidate] = []
    for i, raw in enumerate(raw_candidates):
        edit_list = raw.get("edits", [])
        try:
            # apply edits 到 baseline config 得候选 config（D6a 内存计算）
            candidate_config = edit_ops.apply_edits(baseline_config, edit_list)
            # 取当前 commit（候选无新源码时用 baseline 的 commit）
            from app.compose.git_ops import current_commit
            commit = current_commit()

            candidates.append(Candidate(
                edits=edit_list,
                config=candidate_config,
                source_commit=commit,
            ))
            logger.info("候选 %d 生成：%d 条 edit", i, len(edit_list))
        except Exception:
            logger.exception("候选 %d apply edits 失败，跳过", i)

    return {"candidates": candidates}


def _handle_revision(state: AdaptState, target_idx: int, feedback: str) -> dict:
    """revision 模式（E7a）：修订指定候选。"""
    candidates = list(state.get("candidates", []))
    if target_idx >= len(candidates):
        logger.warning("revision target %d 超出范围（%d 候选）", target_idx, len(candidates))
        return {"candidates": candidates}

    orig = candidates[target_idx]
    # LLM 基于 feedback 修订 edits
    revised_edits = _llm_revise_edits(orig["edits"], feedback)

    try:
        from app.compose.git_ops import current_commit
        candidate_config = edit_ops.apply_edits(state["baseline_config"], revised_edits)
        candidates[target_idx] = Candidate(
            edits=revised_edits,
            config=candidate_config,
            source_commit=current_commit(),
        )
        logger.info("候选 %d 已修订（revision count → %d）", target_idx, state.get("revision_count", 0) + 1)
    except Exception:
        logger.exception("候选 %d revision 失败", target_idx)

    return {
        "candidates": candidates,
        "revision_count": state.get("revision_count", 0) + 1,
        "revision_target": -1,  # 清除 revision 标记
    }


def _llm_generate_edits(landscape: str, baseline_config: dict, round_num: int) -> list[dict]:
    """调 LLM 产出 K 个候选的 edit 指令列表。

    Returns: list of {edits: [{op, target, spec, manifest}]}
    """
    # baseline config 的 processor 概要（不全量，避免 context 爆）
    config_summary = _summarize_config(baseline_config)

    prompt = f"""## Round {round_num} Evolver

## Adaptation Landscape
{landscape}

## 当前 harness 配置概要
{config_summary}

## 你的任务
基于 landscape，产出 {DEFAULT_K} 个候选改进。每个候选是一组 edit 指令。
edit 指令格式：{{"op": "replace|insert|remove", "target": [agent, section, key], "spec": {{class, params}}, "manifest": {{intent, expected_up, expected_down, rationale}}}}

输出 JSON 数组，每个元素 = {{"edits": [edit指令列表]}}。
只输出 JSON，不要其他文字。
"""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    raw = llm.chat(messages) or "[]"

    try:
        result = json.loads(_strip_codeblock(raw))
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "candidates" in result:
            return result["candidates"]
    except json.JSONDecodeError:
        logger.warning("evolver LLM 输出非 JSON: %s", raw[:200])
    return []


def _llm_revise_edits(orig_edits: list[dict], feedback: str) -> list[dict]:
    """调 LLM 基于 critic feedback 修订 edits（E7a revision）。"""
    prompt = f"""## Revision Request

## 原始 edits
{json.dumps(orig_edits, ensure_ascii=False, indent=2)}

## Critic 反馈
{feedback}

## 你的任务
根据反馈修订 edits。保持格式不变。输出修订后的 edits JSON 数组。
"""
    messages = [{"role": "user", "content": prompt}]
    raw = llm.chat(messages) or "[]"
    try:
        return json.loads(_strip_codeblock(raw))
    except json.JSONDecodeError:
        logger.warning("revision LLM 输出非 JSON，用原 edits")
        return orig_edits


def _summarize_config(config: dict) -> str:
    """摘要 baseline config（agent + processor 数量，不全量）。"""
    lines = []
    meta_procs = config.get("meta_pipeline", {}).get("processors", [])
    lines.append(f"meta: {len(meta_procs)} processors ({', '.join(p['group'] for p in meta_procs)})")
    for name, sub in config.get("subagents", {}).items():
        procs = sub.get("processors", [])
        lines.append(f"  {name}: {len(procs)} processors")
    return "\n".join(lines)


def _strip_codeblock(text: str) -> str:
    """剥离 markdown 代码块标记。"""
    t = text.strip()
    if t.startswith("```"):
        import re
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t


_SYSTEM_PROMPT = """你是 HarnessX 的 Evolver。产出 typed builder edits 改进 harness。
每个 edit 必须带 manifest（声明预期效果 + 预期涨/跌的 task + 理由）。
manifest 的诚实声明很重要——Critic 会用它做 reward hacking 防御。
只输出 JSON，不要解释。"""


__all__ = ["evolver"]
