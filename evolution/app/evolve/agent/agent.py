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


async def build_evolve_agent(ctx: EvolveContext):
    """构建进化 Agent（决策 S1/S3/S5/S11 + Phase 2A T1/T2）。

    Phase 2A 改造：加 checkpointer（per-session AsyncSqliteSaver），为对话式
    共创工作台的多轮对话铺地基。thread_id = session_id，LangGraph 据此从
    checkpoint 自动恢复对话史。

    当前仍是单体模式（run_evolve_session 单次 ainvoke），Phase 2B 拆 round 后
    才真正利用多轮对话能力。

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

    # Phase 2A：checkpointer 从 pool 取（per-session，决策 T5）。
    # 对话式共创下每轮 ainvoke 复用同 thread_id 的 checkpoint，自动恢复对话史。
    # 单体模式下 checkpoint 仍会落盘（无害——单次 ainvoke 只产 1 个 checkpoint）。
    from app.evolve.agent.checkpoint_pool import get_checkpoint_pool
    checkpointer = await get_checkpoint_pool().get(ctx.session_id)

    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware_list,
        subagents=None,
        backend=backend,
        checkpointer=checkpointer,
    )
    logger.info(
        "进化 Agent 构建完成: session=%s trace=%s thread_id=%s",
        ctx.session_id, ctx.trace_id, ctx.thread_id,
    )
    return agent


async def run_evolve_session(ctx: EvolveContext, trace_id: str) -> dict[str, Any]:
    """跑一次完整的进化 session（单体 Agent 自主编排，兼容入口）。

    Phase 2B 重构：内部走「inspect round + finalize round」串联（conversing round
    留给 Phase 3 API 触发）。从外部 API 视角行为不变——仍是一锤子跑完。

    Args:
        ctx: 进化上下文（eval_snapshot 已加载评估报告）
        trace_id: 被进化的 trace id

    Returns:
        {"status": "done"|"failed"|"incomplete"|"cancelled", "session_id": ...}
    """
    from app.evolve import db as ev_db

    ctx.trace_id = trace_id
    ctx.session_status = "running"
    ev_db.update_session(ctx.session_id, status="running")

    # 先 create_run 拿自观测 trace_id，再构建 agent（middleware 需要 trace_id）。
    if ctx.recorder:
        handle = ctx.recorder.create_run(
            session_id=ctx.session_id,
            run_purpose="evolution_evolve",
            endpoint="evolve-agent.run",
        )
        ctx.trace_id_self = handle.trace_id

    # ── 阶段 1：inspect round（探查 + 设计 + 落地，单体兼容模式）──
    # 单体模式下 status 保持 running，FlowGuard 不做阶段门控（conversing 才拦），
    # Agent 一气呵成跑完探查→设计→落地→产出。
    agent = await build_evolve_agent(ctx)

    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
    }
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
            ctx.session_status = "pending_review"
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


# ════════════════════════════════════════════════════════════
#  对话式共创 round 函数（Phase 2B，决策 T2/T10）
# ────────────────────────────────────────────────────────────
#  Phase 3 API 改造后，三个 round 各自挂到独立端点：
#    POST /evolve/start         → run_inspect_round（探查 + Agent 开场白）
#    POST /evolve/sessions/:id/messages → run_converse_round（一轮对话）
#    POST /evolve/sessions/:id/finalize → run_finalize_round（落地）
#
#  Phase 2B 阶段：这些函数已就绪但未被 API 调用，靠单元测试保证可用。
# ════════════════════════════════════════════════════════════


async def run_inspect_round(ctx: EvolveContext, trace_id: str) -> dict[str, Any]:
    """探查阶段 round（决策 T2，conversing 之前的准备）。

    流程：
      1. 创建 recorder run + 构建 agent（带 checkpointer + thread_id）
      2. status = running（FlowGuard 不拦，探查工具 + 设计工具可用）
      3. Agent 自动跑：读评估报告 → 读 trace → 探查要素 → 发开场白
         （开场白里总结评估 + 提出本次要讨论的问题，决策 J）
      4. Agent 调 read_eval_report / read_trace / inspect_* 完成探查后，
         自然结束（不进入落地，因为没用户对话）
      5. 探查完成 → status 转 conversing，等用户第一条消息

    与 run_evolve_session 的区别：不跑落地（design_doc/落地编码留给 finalize round）。
    Agent 开场白作为 assistant 消息持久化到 evolve_messages 表（Phase 3 接入时）。

    Returns:
        {"status": "conversing"|"failed"|"cancelled", "session_id": ...}
    """
    from app.evolve import db as ev_db
    from app.evolve.ctx import STATUS_RUNNING, STATUS_CONVERSING, STATUS_FAILED, STATUS_CANCELLED

    ctx.trace_id = trace_id
    ctx.session_status = STATUS_RUNNING
    ev_db.update_session(ctx.session_id, status=STATUS_RUNNING)

    # create_run 拿自观测 trace_id
    if ctx.recorder:
        handle = ctx.recorder.create_run(
            session_id=ctx.session_id,
            run_purpose="evolution_evolve",
            endpoint="evolve-agent.inspect",
        )
        ctx.trace_id_self = handle.trace_id

    agent = await build_evolve_agent(ctx)
    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
    }
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    ctx.emit_log("进化 Agent 启动探查阶段...")
    logger.info("session %s: inspect round 启动 trace=%s", ctx.session_id, trace_id)

    user_input = (
        f"请开始进化流程的探查阶段。trace_id={trace_id}，case_id={ctx.case_id}。\n"
        f"本阶段任务：\n"
        f"1. 调 read_eval_report 读取评估诊断，理解主要问题\n"
        f"2. 调 read_trace 看实际执行流程（对诊断里提到的关键节点）\n"
        f"3. 调 list_elements / read_source 探查 harness 包要素，理解 Agent 当前怎么搭\n"
        f"4. 探查完后，给用户发一条开场白——总结评估发现的主要问题，"
        f"提出本次进化要讨论的核心方向（不要直接 propose 进化点，先让用户了解全貌）\n\n"
        f"重要约束：\n"
        f"- 不要在本阶段调 write_design_doc / write_* / edit_source（落地工具）\n"
        f"- 不要急于 propose 进化点——先让用户了解评估发现，再逐个讨论\n"
        f"- 开场白里清晰说明：发现了什么问题、你建议讨论哪些方向、让用户决定从哪开始"
    )

    try:
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
        )
        # 探查完成，转 conversing 等用户对话
        ctx.session_status = STATUS_CONVERSING
        ev_db.update_session(ctx.session_id, status=STATUS_CONVERSING)
        ctx.emit_log("探查阶段完成，进入对话共创阶段。")
        logger.info("session %s: inspect round 完成，转 conversing", ctx.session_id)
        return {"status": "conversing", "session_id": ctx.session_id}

    except asyncio.CancelledError:
        logger.info("session %s: inspect round 被用户停止", ctx.session_id)
        ev_db.update_session(ctx.session_id, status=STATUS_CANCELLED)
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.cancel_run(ctx.trace_id_self, reason="user_stop")
        return {"status": "cancelled", "session_id": ctx.session_id}
    except Exception as e:
        logger.exception("session %s: inspect round 失败", ctx.session_id)
        ev_db.update_session(ctx.session_id, status=STATUS_FAILED)
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(ctx.trace_id_self, e)
        return {"status": "failed", "error": str(e), "session_id": ctx.session_id}


async def run_converse_round(ctx: EvolveContext, user_message: str) -> dict[str, Any]:
    """对话共创 round（决策 T2，单条用户消息触发一轮）。

    按需触发模型（决策 T2）——每条用户消息启动一次 ainvoke，跑完即止。
    LangGraph 通过 thread_id + checkpoint 自动恢复完整对话史（决策 T1）。
    Agent 在本轮里可以：
      - 自由文本探讨（不进浮窗）
      - 调 propose/update/reject 进化点工具（状态权威变更，进浮窗）
      - 调只读探查工具补充信息
    不能调落地工具（FlowGuard 在 conversing 阶段拦截，决策 T9）。

    Args:
        ctx: 进化上下文（session_status 必须是 conversing）
        user_message: 用户输入的消息内容（markdown）

    Returns:
        {"status": "conversing"|"failed"|"cancelled", "session_id": ...}
    """
    from app.evolve import db as ev_db
    from app.evolve.ctx import STATUS_CONVERSING, STATUS_FAILED, STATUS_CANCELLED

    # 状态校验：只 conversing 状态能跑对话 round
    ctx.reload_session_status()
    if ctx.session_status != STATUS_CONVERSING:
        return {
            "status": "failed",
            "error": f"当前状态 {ctx.session_status} 不能跑对话 round（需 conversing）",
            "session_id": ctx.session_id,
        }

    agent = await build_evolve_agent(ctx)
    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
    }
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    logger.info("session %s: converse round 启动", ctx.session_id)
    ctx.emit_log("用户消息触发对话 round。")

    try:
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config=config,
        )
        logger.info("session %s: converse round 完成", ctx.session_id)
        return {"status": "conversing", "session_id": ctx.session_id}

    except asyncio.CancelledError:
        # 用户停止输出（决策 L）：会话保留，status 不变
        logger.info("session %s: converse round 被用户停止（会话保留）", ctx.session_id)
        return {"status": "cancelled", "session_id": ctx.session_id}
    except Exception as e:
        logger.exception("session %s: converse round 失败", ctx.session_id)
        return {"status": "failed", "error": str(e), "session_id": ctx.session_id}


async def run_finalize_round(ctx: EvolveContext) -> dict[str, Any]:
    """落地 round（决策 T2/T10/D，拍板后触发）。

    流程：
      1. 从 accepted 进化点生成 design_doc.md（决策 T3/U）
      2. status = finalizing（FlowGuard 解锁落地工具）
      3. Agent 跑：按 design_doc 落地（write_*/edit_source）→ validate → change_log
      4. 成功 → pending_review（Phase 3 自动跳 review-report，决策 AA）
         失败 → failed（用户丢弃重开，决策 I）

    无用户交互——一个 finalizing task 跑完即终态（决策 D）。

    Returns:
        {"status": "pending_review"|"failed"|"cancelled", "session_id": ...}
    """
    from app.evolve import db as ev_db
    from app.evolve.ctx import (
        STATUS_FINALIZING, STATUS_PENDING_REVIEW, STATUS_FAILED, STATUS_CANCELLED,
    )
    from app.evolve.docs import generate_design_doc_from_points
    from app.evolve.evolve_repo import EvolvePointsRepo

    # 前置：必须有 accepted 进化点
    if EvolvePointsRepo.count_accepted(ctx.session_id) == 0:
        return {
            "status": "failed",
            "error": "拍板失败：没有 accepted 进化点（至少需要 1 个）",
            "session_id": ctx.session_id,
        }

    # 从 accepted 进化点生成 design_doc
    design_path = generate_design_doc_from_points(ctx.session_id)
    if not design_path:
        return {
            "status": "failed",
            "error": "生成 design_doc 失败（无 accepted 进化点）",
            "session_id": ctx.session_id,
        }
    ctx.design_doc_path = design_path
    ev_db.update_session(ctx.session_id, design_doc_path=design_path)

    # 切到 finalizing（FlowGuard 解锁落地工具）
    ctx.session_status = STATUS_FINALIZING
    ev_db.update_session(ctx.session_id, status=STATUS_FINALIZING)
    ctx.emit_log("进入落地阶段，按已拍板的进化点开始改代码。")

    agent = await build_evolve_agent(ctx)
    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
    }
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    logger.info("session %s: finalize round 启动", ctx.session_id)

    # system 触发消息：指示 Agent 按 design_doc 落地
    accepted = EvolvePointsRepo.list_by_status(ctx.session_id, "accepted")
    user_input = (
        f"用户已拍板 {len(accepted)} 个进化点，design_doc 已生成：{design_path}\n"
        f"现在进入落地阶段。请：\n"
        f"1. 按 design_doc 的改动清单，逐个用 write_*（新建）或 edit_source（修改）落地\n"
        f"2. 全部落地后调 validate_changes 校验\n"
        f"3. 校验后调 write_change_log 产出记录（FlowGuard 要求 design_doc 在前，已满足）\n"
        f"4. 完成后流程结束，进入 pending_review 等用户发布"
    )

    try:
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
        )

        # 产出检查
        if ctx.change_log_path:
            ctx.session_status = STATUS_PENDING_REVIEW
            ev_db.update_session(ctx.session_id, status=STATUS_PENDING_REVIEW)
            ctx.emit_log("落地完成，进入 pending_review 等待发布审查。")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.complete_run(ctx.trace_id_self)
            return {"status": "pending_review", "session_id": ctx.session_id}
        else:
            ctx.emit_log("落地结束但未产出 change_log。")
            ev_db.update_session(ctx.session_id, status=STATUS_FAILED)
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.fail_run(ctx.trace_id_self, "未产出 change_log")
            return {"status": "failed", "error": "未产出 change_log", "session_id": ctx.session_id}

    except asyncio.CancelledError:
        logger.info("session %s: finalize round 被用户停止", ctx.session_id)
        ev_db.update_session(ctx.session_id, status=STATUS_CANCELLED)
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.cancel_run(ctx.trace_id_self, reason="user_stop")
        return {"status": "cancelled", "session_id": ctx.session_id}
    except Exception as e:
        logger.exception("session %s: finalize round 失败", ctx.session_id)
        ev_db.update_session(ctx.session_id, status=STATUS_FAILED)
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


__all__ = [
    "build_evolve_agent",
    "run_evolve_session",
    "run_inspect_round",
    "run_converse_round",
    "run_finalize_round",
]
