"""content 评分类工具（阶段 C：从证据卷宗冻结正文评估）。

内容评估在后台异步跑（按适用组数多次 LLM-judge，较慢），
get_content_score 工具 await 拿结果。后台任务状态由本模块独占持有，
report.py 通过 get_content_task_result 只读访问。

阶段 C 切断：从证据卷宗 facts.deliveries（B1 冻结正文）评估，
不再调 evaluate_trace → extract_deliveries 读工作区文件系统。

约束（循环依赖防线）：本模块不可 import report.py，report.py 单向 import 本模块。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.eval_agent import scoring
from app.eval_agent.ctx import get_eval_context

logger = logging.getLogger("evolution.eval_agent.tools.content")


# ── 后台内容评估任务持有（D9：内容流程并行）──────────────────────
# 每个评估 Agent 实例启动时，启动一个后台 asyncio 任务跑 evaluate_from_facts。
# get_content_score 工具 await 它拿结果。用 dict 按 eval_id 持有（阶段 C：per 评估尝试）。

_content_tasks: dict[str, asyncio.Task] = {}


def _start_content_eval(eval_id: str, facts: dict[str, Any], trace_id: str) -> asyncio.Task:
    """启动后台内容评估任务（D9）。幂等：同 eval_id 只启动一次。"""
    existing = _content_tasks.get(eval_id)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_run_content_eval(facts, trace_id))
    _content_tasks[eval_id] = task
    return task


async def _run_content_eval(facts: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """后台跑 evaluate_from_facts（从卷宗冻结正文，不读工作区）。

    evaluate_from_facts 内含多次同步 httpx 阻塞的 LLM 调用，直接在事件循环里跑
    会阻塞整个循环。用 asyncio.to_thread 丢到线程池执行，事件循环保持响应。
    """
    try:
        result = await asyncio.to_thread(scoring.evaluate_from_facts, facts, trace_id)
        return result or {"skipped": True, "reason": "卷宗无冻结交付物或 LLM 未配置"}
    except Exception as exc:
        logger.exception("内容评估失败 trace=%s", trace_id)
        return {"error": str(exc)}


def clear_content_tasks() -> None:
    """清理后台任务引用（评估 session 结束时调）。

    取消尚未完成的后台评估任务（如总超时强制结束时），避免任务悬挂。
    """
    for task in _content_tasks.values():
        if not task.done():
            task.cancel()
    _content_tasks.clear()


def get_content_task_result(eval_id: str) -> dict[str, Any] | None:
    """读后台内容评估任务的结果（若已完成）。

    供 report.py 在 write_eval_report 时只读访问，避免直接碰 _content_tasks。
    未启动或未完成返回 None。
    """
    task = _content_tasks.get(eval_id)
    if task and task.done():
        try:
            return task.result()
        except Exception:
            return None
    return None


# ── content 类工具 ─────────────────────────────────────────────


def make_content_tools() -> list:
    """构建内容评分类工具。"""

    @tool
    async def get_content_score() -> str:
        """获取 28 维五级锚点评估分数（通用五维 + 按契约适用的领域模块）。

        从证据卷宗的冻结交付物正文评估（不读工作区）。评估在后台异步跑
        （按适用组数多次 LLM-judge，较慢），本工具 await 它拿结果。
        建议在流程诊断做完、写报告前调用。
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        if not ctx.dossier:
            return "错误：证据卷宗未加载，无法评估内容"
        facts = ctx.dossier.get("facts") or {}
        trace_id = ctx.trace_id
        ctx.emit_step("get_content_score", "running", trace_id=trace_id)
        try:
            # 启动后台内容评估（从卷宗冻结正文），await 拿结果
            task = _start_content_eval(ctx.eval_id, facts, trace_id)
            result = await task
            if result.get("skipped"):
                ctx.emit_step("get_content_score", "done", skipped=True)
                return f"内容评估跳过：{result.get('reason')}"
            if result.get("error"):
                ctx.emit_step("get_content_score", "failed", error=result["error"])
                return f"内容评估失败：{result['error']}"
            ctx.emit_step(
                "get_content_score", "done",
                content_overall=result.get("content", {}).get("overall"),
            )
            return f"内容评估完成：\n{json.dumps(result, ensure_ascii=False, indent=2)}"
        except Exception as e:
            ctx.emit_step("get_content_score", "failed", error=str(e))
            return f"取内容分数失败：{e}"

    return [get_content_score]


__all__ = [
    "make_content_tools",
    "clear_content_tasks",
    "get_content_task_result",
]
