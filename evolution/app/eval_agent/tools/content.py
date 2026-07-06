"""content 评分类工具（决策 D9：内容流程并行）。

内容评估在后台异步跑（5 次 LLM-judge，较慢），get_content_score 工具 await 拿结果。
后台任务状态由本模块独占持有，report.py 通过 get_content_task_result 只读访问。

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
# 每个评估 Agent 实例启动时，启动一个后台 asyncio 任务跑 evaluate_trace。
# get_content_score 工具 await 它拿结果。用 dict 按 trace_id 持有。

_content_tasks: dict[str, asyncio.Task] = {}


def _start_content_eval(trace_id: str) -> asyncio.Task:
    """启动后台内容评估任务（D9）。幂等：同 trace 只启动一次。"""
    existing = _content_tasks.get(trace_id)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_run_content_eval(trace_id))
    _content_tasks[trace_id] = task
    return task


async def _run_content_eval(trace_id: str) -> dict[str, Any]:
    """后台跑 evaluate_trace（复用现有内容评估引擎）。

    evaluate_trace 内含 5 次同步 httpx 阻塞的 LLM 调用，直接在事件循环里跑
    会阻塞整个循环（SSE 心跳、其他请求全卡）。用 asyncio.to_thread 丢到
    线程池执行，事件循环保持响应。
    """
    try:
        result = await asyncio.to_thread(scoring.evaluate_trace, trace_id)
        return result or {"skipped": True, "reason": "无 writing 正文或 LLM 未配置"}
    except Exception as exc:
        logger.exception("内容评估失败 %s", trace_id)
        return {"error": str(exc)}


def clear_content_tasks() -> None:
    """清理后台任务引用（评估 session 结束时调）。

    取消尚未完成的后台评估任务（如总超时强制结束时），避免任务悬挂。
    """
    for task in _content_tasks.values():
        if not task.done():
            task.cancel()
    _content_tasks.clear()


def get_content_task_result(trace_id: str) -> dict[str, Any] | None:
    """读后台内容评估任务的结果（若已完成）。

    供 report.py 在 write_eval_report 时只读访问，避免直接碰 _content_tasks。
    未启动或未完成返回 None。
    """
    task = _content_tasks.get(trace_id)
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
        """获取内容质量层评估分数（内容8维 + subagent4维）。

        内容评估在后台异步跑（5 次 LLM-judge，较慢），本工具 await 它拿结果。
        如果还没跑完会等待。建议在流程诊断做完、写报告前调用。
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        trace_id = ctx.trace_id
        ctx.emit_step("get_content_score", "running", trace_id=trace_id)
        try:
            # 启动后台内容评估（D9：内容流程并行），await 拿结果
            task = _start_content_eval(trace_id)
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
