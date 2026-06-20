"""prompt 优化器（Phase 2 T2.2，决策 D8）。

职责：取候选记录（诊断结论 + 原 prompt + 维度 rubric）→ LLM 生成改进版 prompt
→ 存为新版本（source=optimized，label=candidate）→ 回填 improvement_candidates.candidate_version_id。

专用优化器 prompt（D8）：输入=诊断+原prompt+rubric，输出=完整改进版 prompt。
优化器本身是普通文本 prompt（非 prompts 表管理，硬编码于此，简单可控）。

自动连锁：diagnosis 生成候选记录后立即调 optimize_candidate 生成版本。
"""

from __future__ import annotations

import logging
from typing import Any

import app.db as db
from app import llm

logger = logging.getLogger("monitoring.optimizer")


def optimize_candidate(candidate_id: int) -> dict[str, Any] | None:
    """对一条 improvement_candidate 生成候选 prompt 版本。

    流程：取候选的(诊断+原prompt+rubric) → LLM 生成改进版 → 存新版本(label=candidate)
    → 回填 candidate_version_id → 更新 status=optimized。

    Returns: {candidate_id, prompt_name, new_version, version_id} 或 None（失败）。
    """
    if not llm.judge_enabled():
        logger.warning("optimize_candidate 跳过：LLM 未配置")
        return None

    cand = db.query_one("SELECT * FROM improvement_candidates WHERE id=?", (candidate_id,))
    if cand is None:
        logger.warning("optimize_candidate: 候选不存在 %s", candidate_id)
        return None
    if cand["candidate_version_id"] is not None:
        # 已生成过候选版本（幂等）
        return {"candidate_id": candidate_id, "prompt_name": cand["prompt_name"],
                "version_id": cand["candidate_version_id"], "skipped": True}

    prompt_name = cand["prompt_name"]
    diagnosis = cand["diagnosis"] or ""

    # 1. 取原 prompt（production 版本）
    original_content = _get_prompt_content(prompt_name)
    if original_content is None:
        logger.warning("optimize_candidate: prompt %s 无 production 内容", prompt_name)
        return None

    # 2. 取该维度 rubric（让优化器知道目标标准）
    rubric_text = _get_dimension_rubric(cand["layer"], cand["target"], cand.get("metric", ""))

    # 3. LLM 生成改进版 prompt
    improved = _generate_improved_prompt(
        original=original_content, diagnosis=diagnosis, rubric=rubric_text,
        prompt_name=prompt_name, metric=cand.get("metric", ""),
    )

    if not improved or not improved.strip():
        logger.warning("optimize_candidate: LLM 返回空改进版 %s", candidate_id)
        return None

    # 4. 存为新版本（source=optimized, label=candidate）
    version_info = _create_candidate_version(prompt_name, improved, diagnosis, candidate_id)
    if version_info is None:
        return None

    # 5. 回填 improvement_candidates
    db.execute(
        "UPDATE improvement_candidates SET candidate_version_id=?, status='optimized' WHERE id=?",
        (version_info["version_id"], candidate_id),
    )
    return {
        "candidate_id": candidate_id, "prompt_name": prompt_name,
        "version": version_info["version"], "version_id": version_info["version_id"],
    }


_OPTIMIZER_PROMPT = """你是 prompt 工程专家。下面是一个写作 Agent 子代理的 system prompt，
它在某次执行中暴露了一个质量缺陷。请基于诊断结论，产出一份**改进版完整 prompt**。

## 要求
1. 保持原 prompt 的整体结构和职责，只针对诊断指出的缺陷做改进
2. 改进要具体、可执行（明确加了什么约束/指令/示例）
3. 输出**完整的改进版 prompt**（可直接替换原 prompt 使用），不要输出分析过程
4. 不要加任何解释性前言（如"以下是改进版"），直接输出 prompt 正文

## 目标评估标准（改进要让产出更符合此标准）
{rubric}

## 诊断结论（本次执行暴露的问题）
{diagnosis}

## 原 prompt（{prompt_name}）
{original}

## 请直接输出改进版完整 prompt："""


def _generate_improved_prompt(
    original: str, diagnosis: str, rubric: str, prompt_name: str, metric: str,
) -> str:
    """调 LLM 生成改进版 prompt。"""
    prompt_text = _OPTIMIZER_PROMPT.format(
        rubric=rubric or f"(目标维度：{metric})", diagnosis=diagnosis or "(无诊断)",
        prompt_name=prompt_name, original=original,
    )
    messages = [{"role": "user", "content": prompt_text}]
    raw = llm.chat(messages, temperature=0.3)  # 略高温度促进改写多样性
    # 清理：去掉可能的 markdown 代码块包裹
    text = raw.strip()
    if text.startswith("```"):
        import re
        text = re.sub(r"^```(?:markdown|text)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _get_prompt_content(prompt_name: str) -> str | None:
    """取某 prompt 的 production 版本内容。"""
    try:
        import app.prompts_repo as repo
        result = repo.get_prompt_content(prompt_name, repo.PRODUCTION_LABEL)
        return result["content"] if result else None
    except Exception:
        return None


def _get_dimension_rubric(layer: str, target: str, metric: str) -> str:
    """取该维度的 rubric 文本（让优化器知道目标标准）。"""
    from app.rubrics import xianxia as rubric
    if layer == "content":
        dim = next((d for d in rubric.CONTENT_DIMENSIONS if d["key"] == metric), None)
    else:
        dim = next((d for d in rubric.SUBAGENT_DIMENSIONS if d["agent"] == target), None)
    if dim is None:
        return ""
    levels = "\n".join(f"- {s}：{d}" for s, d in sorted(dim["levels"].items()))
    return f"{dim['key']}：{dim['question']}\n分档：\n{levels}"


# candidate label（与 production/latest 并列，A/B 回放时用）
CANDIDATE_LABEL = "candidate"


def _create_candidate_version(
    prompt_name: str, content: str, commit_message: str, candidate_id: int,
) -> dict[str, Any] | None:
    """创建候选版本（source=optimized, label=candidate）。

    复用 prompts_repo.create_version。candidate label 互斥（同 prompt 只有一个 candidate）。
    """
    try:
        import app.prompts_repo as repo
        prompt = repo.get_prompt_by_name(prompt_name)
        if prompt is None:
            # prompt 线不存在：先创建（首次优化场景）
            repo.create_prompt(prompt_name, "text")
            prompt = repo.get_prompt_by_name(prompt_name)
            if prompt is None:
                return None
        version = repo.create_version(
            prompt_id=prompt["id"], content=content,
            commit_message=f"优化候选(候选#{candidate_id}): {commit_message[:100]}",
            source="optimized", labels=[CANDIDATE_LABEL],
        )
        return {"version": version["version"], "version_id": version["id"]}
    except Exception:
        logger.exception("创建候选版本失败 %s", prompt_name)
        return None
