"""content 评分类工具（阶段 C：从证据卷宗冻结正文评估；DEC-002 重写）。

内容评估在后台异步跑（按适用组数多次 LLM-judge，较慢），
get_content_score 工具 await 拿结果。后台任务状态由本模块独占持有，
report.py 通过 get_content_task_result 只读访问。

DEC-002 关键约束（FR-003 / NFR-001）：
  - 最多两组并发、每组单次 60s、失败组最多重试一次、150s 总预算；
  - 已成功/终态组复用缓存，成功组重算次数为 0；
  - 评估 Trace 记录每组开始/超时/重试/终态（CON-001）；
  - 结果机器可读（complete / failed_groups），错误不再伪装成工具成功（CON-003）。

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
from app.trace.observers import TraceLlmObserver

logger = logging.getLogger("evolution.eval_agent.tools.content")


# ── 后台内容评估任务持有（D9：内容流程并行）──────────────────────
# 每个评估 Agent 实例启动时，启动一个后台 asyncio 任务跑 evaluate_content_groups。
# get_content_score 工具 await 它拿结果。用 dict 按 eval_id 持有（阶段 C：per 评估尝试）。

_content_tasks: dict[str, asyncio.Task] = {}


def _start_content_eval(
    eval_id: str, facts: dict[str, Any], trace_id: str, observer: TraceLlmObserver | None,
) -> asyncio.Task:
    """启动后台内容评估任务（D9 + DEC-002）。幂等：同 eval_id 只启动一次。

    已有未完成任务则复用（Agent 重复调用 get_content_score 不启动第二轮计算，
    由 evaluate_content_groups 的组级缓存保证成功组重算次数为 0）。
    """
    existing = _content_tasks.get(eval_id)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_run_content_eval(facts, trace_id, eval_id, observer))
    _content_tasks[eval_id] = task
    return task


async def _run_content_eval(
    facts: dict[str, Any], trace_id: str, eval_id: str, observer: TraceLlmObserver | None,
) -> dict[str, Any]:
    """后台跑 evaluate_content_groups（DEC-002：并发/预算/重试/缓存 + Trace 观测）。

    evaluate_content_groups 内部用 asyncio.Semaphore + asyncio.to_thread 把每次同步
    httpx 阻塞的 LLM 调用丢线程池，事件循环保持响应。
    """
    try:
        result = await scoring.evaluate_content_groups(
            facts, trace_id, eval_id=eval_id, observer=observer,
        )
        return result or {"skipped": True, "reason": "卷宗无冻结交付物或 LLM 未配置"}
    except Exception as exc:
        logger.exception("内容评估失败 trace=%s", trace_id)
        return {"error": str(exc), "complete": False}


def clear_content_tasks(eval_id: str | None = None) -> None:
    """清理后台任务引用 + 组级缓存（评估 session 结束时调）。

    eval_id 非空时只清该 session 的任务与缓存——并发跑多个评估 Agent 时，
    一个 session 结束不能误清其他 session 的成功组缓存（否则违反 DEC-002
    「成功组重算次数为 0」）。eval_id=None（如全局停服）才全清。
    """
    if eval_id is None:
        for task in _content_tasks.values():
            if not task.done():
                task.cancel()
        _content_tasks.clear()
        scoring.clear_content_cache()
        return
    task = _content_tasks.pop(eval_id, None)
    if task is not None and not task.done():
        task.cancel()
    scoring.clear_content_cache(eval_id)


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
        # DEC-001：把评估 Trace 观测桥传给内容评分，让每组 LLM 调用可见。
        observer = (
            TraceLlmObserver(ctx.recorder, ctx.trace_id_self, component="content-scoring")
            if ctx.recorder and ctx.trace_id_self else None
        )
        try:
            # 启动后台内容评估（从卷宗冻结正文），await 拿结果。
            # 已启动则复用任务（组级缓存保证成功组不重算）。
            task = _start_content_eval(ctx.eval_id, facts, trace_id, observer)
            result = await task
            if result.get("skipped"):
                ctx.emit_step("get_content_score", "done", skipped=True)
                return f"内容评估跳过：{result.get('reason')}"
            if result.get("error"):
                # CON-003：返回机器可读的失败结论，而非普通字符串让框架记 tool completed。
                ctx.emit_step(
                    "get_content_score", "failed",
                    error=result["error"], failed_groups=result.get("failed_groups", []),
                )
                return (
                    f"内容评估失败（complete=false）：{result['error']}"
                    f"\n失败组：{result.get('failed_groups', [])}"
                    "\n本次评估因内容评分失败不得封存评估卷宗。"
                )
            if not result.get("complete"):
                # 部分组失败：明确机器可读结论（CON-003）。
                ctx.emit_step(
                    "get_content_score", "done",
                    complete=False, failed_groups=result.get("failed_groups", []),
                    content_overall=None,
                )
                return (
                    f"内容评估未全部完成（complete=false）：失败组 {result.get('failed_groups', [])}"
                    f"\n{json.dumps(result.get('groups', {}), ensure_ascii=False, indent=2)}"
                    "\n缺失维度评估的卷宗不得封存（DEC-002 严格完整性）。"
                )
            ctx.emit_step(
                "get_content_score", "done",
                complete=True,
                content_overall=result.get("content", {}).get("overall"),
                computation_hash=result.get("computation_hash"),
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
