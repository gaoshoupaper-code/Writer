"""单体进化 Agent 构建 + 运行（决策 S1/S3/S5/S11）。

重构后的单体进化 Agent，替代原 driver + plan/execute 三体结构：
  - 单体 Agent（无子代理），自己一把跑完：探查 → 设计 → 落地 → 校验 → 产出
  - 15 工具（4 探查 + 5 写 + 1 edit + 5 流程）
  - middleware：NoFilesystemToolsMiddleware（禁框架 fs）+ FlowGuardMiddleware（产出约束）+ TraceMiddleware（自观测）
  - backend：FilesystemBackend（专用写工具内部调用，virtual_mode 路径安全）

架构（S1/S3）：
  create_deep_agent(
      tools=make_evolve_tools(backend),       # 15 工具
      subagents=None,                          # 单体无子代理
      middleware=[NoFS, FlowGuard, Trace?],    # 禁 fs + 产出约束 + trace
      backend=FilesystemBackend(...),          # 写工具落盘
  )

输入：trace_id + 评估报告（从 evaluation_sessions 表加载到 ctx.eval_snapshot）
产出：harnesses/current/ 代码改动 + design_doc.md + change_log.md → 待审（pending_review）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.errors import GraphRecursionError

from app.core.settings import settings
from app.common.middleware.no_fs import NoFilesystemToolsMiddleware
from app.common.model_factory import build_agent_model
from app.evolve.agent.middleware.flow_guard import FlowGuardMiddleware
from app.evolve.agent.prompt import evolve_system_prompt, render_memory_section
from app.evolve.agent.tools import make_evolve_tools
from app.evolve.ctx import EvolveContext, set_tool_context
from app.trace import TraceMiddleware, TraceCallbackHandler

logger = logging.getLogger("evolution.evolve.agent")

# 不设总超时护栏（asyncio.wait_for）——进化时长不设上限，让它自然跑完。
# 不设 recursion_limit 步数限制：步数交给框架默认（≈10007，事实上的不限制），
# 避免正常进化因步数上限被误杀。GraphRecursionError 分支仅作极端死循环的防御性兜底。


def build_evolve_agent(ctx: EvolveContext):
    """构建单体进化 Agent（决策 S1/S3/S5/S11）。

    Args:
        ctx: 进化上下文（trace_id + eval_snapshot 已作为输入填入）

    Returns:
        编译后的 CompiledStateGraph（可 ainvoke/astream）
    """
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    set_tool_context(ctx)

    model = build_agent_model(temperature=0.2)

    # FilesystemBackend：专用写工具内部调用它落盘到 harnesses/current/
    # virtual_mode=True：root_dir 作为虚拟根，阻止绝对路径 / .. 越界（S5/S13）
    backend = FilesystemBackend(
        root_dir=str(settings.harness_work_dir_path),
        virtual_mode=True,
    )

    # 15 工具（inspect 4 + writers 6 + flow 5），writers 需 backend
    tools = make_evolve_tools(backend=backend)

    system_prompt = evolve_system_prompt(
        session_id=ctx.session_id,
        trace_id=ctx.trace_id,
        eval_summary=_format_eval_summary(ctx),
        reflections_summary=_format_reflections(ctx),
        memory_section=_format_memory_section(ctx),
    )

    # middleware：禁框架 fs + 产出约束 + 自观测 trace
    trace_middleware = TraceMiddleware(
        recorder=ctx.recorder,
        trace_id=ctx.trace_id_self,
        agent_name="evolve-agent",
    ) if ctx.recorder and ctx.trace_id_self else None

    middleware_list: list = [
        NoFilesystemToolsMiddleware(),
        FlowGuardMiddleware(),
    ]
    if trace_middleware:
        middleware_list.append(trace_middleware)

    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware_list,
        subagents=None,
        backend=backend,
        checkpointer=None,
    )
    logger.info(
        "单体进化 Agent 构建完成: session=%s trace=%s",
        ctx.session_id, ctx.trace_id,
    )
    return agent


async def run_evolve_session(ctx: EvolveContext, trace_id: str) -> dict[str, Any]:
    """跑一次完整的进化 session（单体 Agent 自主编排）。

    Args:
        ctx: 进化上下文（eval_snapshot 已加载评估报告）
        trace_id: 被进化的 trace id

    Returns:
        {"status": "done"|"failed"|"incomplete"|"cancelled", "session_id": ...}
    """
    from app.evolve import db as ev_db

    ctx.trace_id = trace_id
    ctx.review_status = "running"
    ev_db.update_session(ctx.session_id, status="running")

    # 先 create_run 拿自观测 trace_id，再构建 agent（middleware 需要 trace_id）。
    if ctx.recorder:
        handle = ctx.recorder.create_run(
            session_id=ctx.session_id,
            run_purpose="evolution_evolve",
            endpoint="evolve-agent.run",
        )
        ctx.trace_id_self = handle.trace_id

    agent = build_evolve_agent(ctx)

    # config 注入 TraceCallbackHandler（构建调用树）。
    config: dict[str, Any] = {}
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    ctx.emit_log("单体进化 Agent 启动，开始自主编排...")
    logger.info("session %s: 进化 Agent 启动 trace=%s", ctx.session_id, trace_id)

    user_input = (
        f"请开始进化流程。trace_id={trace_id}，case_id={ctx.case_id}。"
        f"按 system prompt 的建议流程：读评估报告 → 读 trace → 探查要素 → "
        f"设计改进方案（write_design_doc）→ 落地改动（write_*/edit_source）→ "
        f"校验（validate_changes）→ 产出记录（write_change_log）。"
        f"注意：评估报告已加载到上下文（read_eval_report 可读）。"
    )

    try:
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
        )

        logger.info("session %s: 进化 Agent 执行完成", ctx.session_id)

        # 产出检查：design_doc + change_log 都齐才算完成。
        if ctx.change_log_path and ctx.design_doc_path:
            ctx.review_status = "pending_review"
            ev_db.update_session(ctx.session_id, status="pending_review")
            ctx.emit_log("进化流程完成，改动已落地，等待人工 review 发版。")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.complete_run(ctx.trace_id_self)
            return {"status": "done", "session_id": ctx.session_id}
        else:
            # 区分失败原因
            if not ctx.design_doc_path:
                ctx.emit_log("Agent 结束但未产出 design_doc（方案设计未完成）。")
                fail_reason = "未产出 design_doc"
            else:
                ctx.emit_log("Agent 结束但未产出 change_log（改动记录未完成）。")
                fail_reason = "未产出 change_log"
            ev_db.update_session(ctx.session_id, status="failed")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.fail_run(ctx.trace_id_self, fail_reason)
            return {"status": "incomplete", "session_id": ctx.session_id}

    except GraphRecursionError:
        # 步数触顶（仅当框架默认上限被触达时出现）：
        # 模型陷入死循环没收敛（反复调工具不收尾）。
        logger.warning(
            "session %s: 进化 Agent 步数触顶（框架默认上限），未收敛", ctx.session_id
        )
        ctx.emit_log(
            "进化 Agent 消耗了过多步数仍未完成"
            "（可能反复调用工具未收尾）。请重试，或检查模型是否稳定。"
        )
        ev_db.update_session(ctx.session_id, status="failed")
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(ctx.trace_id_self, "进化 Agent 步数触顶（未收敛）")
        return {
            "status": "failed", "session_id": ctx.session_id,
            "error": "进化 Agent 步数触顶（未收敛）",
        }
    except asyncio.CancelledError:
        # 用户手动停止（stop 端点调 task.cancel）：ainvoke 在某个 await 点被中断。
        # 推进 cancelled 终态 + recorder 收尾。不 re-raise——否则会被 _run_evolve_bg
        # 的 except Exception 当失败处理，覆盖刚标的 cancelled。
        logger.info("session %s: 进化 Agent 被用户停止", ctx.session_id)
        ctx.emit_log("进化已被手动停止。")
        ev_db.update_session(ctx.session_id, status="cancelled")
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.cancel_run(ctx.trace_id_self, reason="user_stop")
        return {"status": "cancelled", "session_id": ctx.session_id}
    except Exception as e:
        logger.exception("session %s: 进化 Agent 执行失败", ctx.session_id)
        ev_db.update_session(ctx.session_id, status="failed")
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(ctx.trace_id_self, e)
        return {"status": "failed", "error": str(e), "session_id": ctx.session_id}


# ── prompt 摘要辅助（从 driver/agent.py 搬来）─────────────────────


def _format_eval_summary(ctx: EvolveContext) -> str:
    """把评估报告快照格式化成 system prompt 的摘要。"""
    snap = ctx.eval_snapshot
    if not snap:
        return "(未加载评估报告)"
    findings = snap.get("findings") or []
    scores = snap.get("scores") or {}
    lines = [
        f"- trace_id: {snap.get('trace_id', '?')}",
        f"- 诊断条目数: {len(findings)}",
    ]
    # 数据闭环 F1：数据集层标注（golden 验证 / growing 探索），指导进化模式。
    if ctx.origin_layer:
        if ctx.origin_layer == "golden":
            lines.append("- 数据集层: golden（验证模式——改进后不能在 golden 集上退化）")
        else:
            lines.append("- 数据集层: growing（探索模式——用于发现新问题/新方向）")
    # 摘要前几个高 severity finding
    high = [f for f in findings if isinstance(f, dict) and f.get("severity") == "high"]
    if high:
        lines.append(f"- 高优先级问题（{len(high)} 条）:")
        for h in high[:3]:
            lines.append(f"  • [{h.get('dimension', '?')}] {h.get('finding', '')[:80]}")
    content = scores.get("content", {})
    if isinstance(content, dict) and content.get("content", {}).get("overall") is not None:
        lines.append(f"- 内容层 overall: {content['content']['overall']}")
    return "\n".join(lines)


def _format_reflections(ctx: EvolveContext) -> str:
    """从反思库提取与当前评估问题相关的失败模式，格式化为 prompt 摘要。

    按 eval_snapshot.findings 的 dimension 查相关反思，每类取 top 3。
    无反思或查询失败返回空串（prompt 里不渲染反思段）。
    """
    try:
        from app.reflection import repo as reflection_repo
    except ImportError:
        return ""

    snap = ctx.eval_snapshot
    if not snap:
        return ""
    findings = snap.get("findings") or []
    categories: list[str] = []
    for f in findings:
        if isinstance(f, dict) and f.get("dimension"):
            dim = f["dimension"]
            if dim not in categories:
                categories.append(dim)

    if not categories:
        reflections = reflection_repo.list_all(limit=5)
    else:
        reflections = reflection_repo.list_by_categories(categories, limit_per_category=3)

    # P4：记忆失败模式（recall_miss/retrieval_fail）不是 eval dimension，
    # 上面按 dimension 查会遗漏。这里追加查记忆类别，确保 evolution agent 能看到。
    memory_categories = ["recall_miss", "retrieval_fail", "extraction_gap",
                         "temporal_violation", "epistemic_violation", "promise_orphan"]
    existing_ids = {r.get("id") for r in reflections}
    for mc in memory_categories:
        mem_reflections = reflection_repo.list_by_categories([mc], limit_per_category=2)
        for r in mem_reflections:
            if r.get("id") not in existing_ids:
                reflections.append(r)
                existing_ids.add(r.get("id"))

    if not reflections:
        return ""

    lines = [f"（共 {len(reflections)} 条历史失败模式）"]
    for r in reflections[:10]:
        hit = r.get("hit_count", 0)
        lines.append(f"  • [{r['category']}] (命中{hit}次) {r['pattern'][:120]}")
    return "\n".join(lines)


def _format_memory_section(ctx: EvolveContext) -> str:
    """探测当前 harness 工作副本是否有记忆要素，有则渲染记忆子系统认知节。

    认定"哪些是记忆要素"与 elements_api 同源——都读 versioning.constants.MEMORY_FILES。
    探测逻辑：检查 settings.harness_work_dir_path 下 MEMORY_FILES 的文件是否存在，
    任意一个存在即认定该包有记忆子系统（注入认知节），全无则返回空串（老版本兼容）。

    注意与桌面的数据源差异：这里探测的是【工作副本】（Agent 在上面做改动），
    桌面 memory-elements 接口查的是【git 快照】（已发布版本）。两者数据源不同是合理的——
    Agent 在工作副本上进化，桌面展示历史版本。
    """
    from app.versioning.constants import MEMORY_FILES

    work_dir = settings.harness_work_dir_path
    has_memory = any((work_dir / path).exists() for path in MEMORY_FILES)
    if not has_memory:
        return ""
    return render_memory_section()


__all__ = ["build_evolve_agent", "run_evolve_session"]
