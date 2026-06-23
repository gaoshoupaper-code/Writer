"""badcase 诊断器（Phase 2 T2.1）。

职责：badcase flagged 维度 → 查 agent_prompt_map 定位 prompt → LLM 生成
诊断结论（为什么这个 prompt 导致了这个维度低分）→ 写 improvement_candidates。

自动连锁（决策：自动连锁）：evaluate_trace 判定 badcase 后自动调 diagnose_badcase。
诊断失败不阻塞评估主流程（静默降级，候选可在 API 手动重试）。

设计依据：设计文档 D6（配置表归因）+ D8（专用优化器）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.core import llm

logger = logging.getLogger("evolution.diagnosis")


def diagnose_badcase(trace_id: str, badcase_result: dict[str, Any]) -> list[dict[str, Any]]:
    """对 badcase 的每个 flagged 维度生成诊断，写 improvement_candidates。

    Args:
        trace_id: trace id
        badcase_result: evaluate_trace 返回的 badcase 字段（含 flagged_dimensions）

    Returns: 生成的候选记录列表（含 id/prompt_name/diagnosis）。
    """
    if not badcase_result.get("is_badcase"):
        return []
    if not llm.judge_enabled():
        logger.warning("diagnose_badcase 跳过：LLM 未配置")
        return []

    candidates: list[dict[str, Any]] = []
    for flagged in badcase_result.get("flagged_dimensions", []):
        try:
            cand = _diagnose_one(trace_id, flagged)
            if cand:
                candidates.append(cand)
        except Exception:
            logger.exception("诊断失败 %s %s", trace_id, flagged.get("metric"))
    return candidates


def _diagnose_one(trace_id: str, flagged: dict[str, Any]) -> dict[str, Any] | None:
    """诊断单个 flagged 维度：定位 prompt → LLM 分析 → 写候选记录。"""
    layer = flagged["layer"]
    target = flagged["target"]
    metric = flagged["metric"]
    score = flagged["score"]

    # 1. 定位 prompt（查 agent_prompt_map）
    # subagent 层 → 该 agent 的 primary prompt；内容层 → writing 的 primary prompt
    agent_name = target if layer == "subagent" else "writing"
    prompt_name = _locate_prompt(agent_name)
    if prompt_name is None:
        logger.warning("诊断跳过：无法定位 %s 的 prompt", agent_name)
        return None

    # 2. 取该 prompt 的 production 内容
    prompt_content = _get_prompt_content(prompt_name)
    if prompt_content is None:
        logger.warning("诊断跳过：prompt %s 无内容", prompt_name)
        return None

    # 3. 取该维度的评估证据
    evidence = _get_evidence(trace_id, layer, target, metric)

    # 4. LLM 生成诊断结论
    diagnosis = _generate_diagnosis(
        metric=metric, score=score, evidence=evidence,
        prompt_name=prompt_name, prompt_content=prompt_content,
    )

    # 5. 写 improvement_candidates（status=pending，候选版本 Phase 2 T2.2 生成）
    now = datetime.now(UTC).isoformat()
    cur = db.execute(
        """INSERT INTO improvement_candidates
           (trace_id, layer, target, prompt_name, diagnosis, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (trace_id, layer, target, prompt_name, diagnosis, now),
    )
    return {
        "id": cur.lastrowid, "trace_id": trace_id, "layer": layer,
        "target": target, "metric": metric, "score": score,
        "prompt_name": prompt_name, "diagnosis": diagnosis,
    }


def _locate_prompt(agent_name: str) -> str | None:
    """查 agent_prompt_map 定位该 agent 的 primary prompt。"""
    row = db.query_one(
        "SELECT prompt_name FROM agent_prompt_map WHERE agent_name=? AND role='primary'",
        (agent_name,),
    )
    return row["prompt_name"] if row else None


def _get_prompt_content(prompt_name: str) -> str | None:
    """取某 prompt 的 production 版本内容（从 prompts_repo）。"""
    try:
        import app.improvement.prompts_repo as repo
        result = repo.get_prompt_content(prompt_name, repo.PRODUCTION_LABEL)
        return result["content"] if result else None
    except Exception:
        # evolution 的 prompts 表可能还没导入这些 prompt（首次运行）
        # 降级：返回占位，诊断仍能跑（LLM 拿不到原 prompt 会基于维度泛泛诊断）
        return None


def _get_evidence(trace_id: str, layer: str, target: str, metric: str) -> str:
    """取该维度的评估证据（judge 打分时给的依据）。"""
    row = db.query_one(
        "SELECT evidence FROM evaluation_scores WHERE trace_id=? AND layer=? AND target=? AND metric=?",
        (trace_id, layer, target, metric),
    )
    return row["evidence"] if row else ""


_DIAGNOSIS_PROMPT = """你是写作 Agent 系统的 prompt 诊断专家。

下面是一次 agent 执行中，某个评估维度被判为 badcase（低分）的情况。
请分析：**这个低分最可能是因为哪个 prompt 的什么缺陷导致的？应该如何改进这个 prompt？**

## badcase 信息
- 评估维度：{metric}
- 得分：{score}（满分 1.0）
- judge 给出的打分依据：{evidence}

## 当前 prompt（{prompt_name}）
{prompt_content}

请输出诊断结论，格式为纯文本（不要 JSON、不要 markdown 代码块），包含：
1. 根因分析：这个 prompt 的哪个具体表述/缺失导致了低分？
2. 改进建议：应该怎么改这个 prompt 来提升该维度？
控制在 300 字以内。"""


def _generate_diagnosis(
    metric: str, score: float, evidence: str,
    prompt_name: str, prompt_content: str | None,
) -> str:
    """调 LLM 生成诊断结论。"""
    prompt_text = _DIAGNOSIS_PROMPT.format(
        metric=metric, score=score, evidence=evidence or "(无)",
        prompt_name=prompt_name,
        prompt_content=prompt_content or "(prompt 内容未导入 evolution，请基于维度泛泛分析)",
    )
    messages = [{"role": "user", "content": prompt_text}]
    return llm.chat(messages).strip()


def list_candidates(status: str | None = None) -> list[dict[str, Any]]:
    """列出 improvement_candidates（供 API/页面）。"""
    if status:
        rows = db.query_all(
            "SELECT * FROM improvement_candidates WHERE status=? ORDER BY id DESC", (status,)
        )
    else:
        rows = db.query_all("SELECT * FROM improvement_candidates ORDER BY id DESC")
    return [dict(r) for r in rows]
