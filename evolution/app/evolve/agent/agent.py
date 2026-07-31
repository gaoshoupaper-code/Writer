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
from app.evolve.agent.prompt import evolve_system_prompt
from app.evolve.agent.tools import make_evolve_tools
from app.evolve.ctx import EvolveContext, set_tool_context
from app.trace import TraceMiddleware, TraceCallbackHandler
from app.trace.facts import add_lineage
from contracts.trace import TraceSpanLink

logger = logging.getLogger("evolution.evolve.agent")


def _start_evolution_trace(ctx: EvolveContext, endpoint: str) -> None:
    """为一次进化工作流创建唯一 Trace；后续对话与落地轮次复用它。"""
    # [诊断] 确认守卫命中情况
    logger.warning(
        "[诊断] _start_evolution_trace 入口: session=%s recorder=%s trace_id_self=%r",
        ctx.session_id, ctx.recorder is not None, ctx.trace_id_self,
    )
    if not ctx.recorder or ctx.trace_id_self:
        logger.warning(
            "[诊断] _start_evolution_trace 提前 return: session=%s 原因=%s",
            ctx.session_id,
            "no_recorder" if not ctx.recorder else "trace_id_self_already_set",
        )
        return
    evaluation_trace_id = str(ctx.eval_dossier.get("evaluation_trace_id") or "")
    handle = ctx.recorder.create_run(
        session_id=ctx.session_id,
        run_purpose="evolution_evolve",
        endpoint=endpoint,
        session_type="evolve",
        workload="evolution",
        links=[TraceSpanLink(
            target_trace_id=evaluation_trace_id,
            relation="consumes",
            artifact={"type": "evaluation_dossier", "id": ctx.eval_dossier_id},
        )],
        external_refs={
            "experiment_id": f"experiment-{ctx.session_id}",
            "evaluation_id": str(ctx.eval_dossier.get("eval_attempt_id") or ""),
            "evaluation_dossier_id": ctx.eval_dossier_id,
        },
    )
    ctx.trace_id_self = handle.trace_id
    # [诊断] 确认赋值成功
    logger.warning(
        "[诊断] _start_evolution_trace 已赋值: session=%s trace_id_self=%s",
        ctx.session_id, ctx.trace_id_self,
    )
    try:
        add_lineage(
            "trace", ctx.trace_id_self, "consumes",
            "evaluation_dossier", ctx.eval_dossier_id,
        )
    except Exception as exc:
        ctx.recorder.fail_run(ctx.trace_id_self, exc)
        raise


async def _run_agent_streamed(
    agent: Any,
    user_input: str,
    config: dict[str, Any],
    ctx: "EvolveContext",
) -> None:
    """astream_events + EvolveEventSink → 消息持久化 + 通知帧（trace 重构 20260720_154825）。

    替代原 Phase 6 的 token 级流式桥接。重大变更：
      - 不再把 sink 帧当 sse_frame 入库（trace 通道不再背 token 流职责）
      - sink 帧改为派生两类副作用：
        1. 持久化消息到 evolve_messages（assistant / tool / system）—— 权威消息源
        2. 通知帧（type=model_output/tool_output）走 run_meta 通道通知前端：消息更新了，
           前端 Pull 拉到后调 loadMessages 拉权威消息即可

    设计（D1/D2/D3）：
      - trace 通道只存 span（TraceMiddleware 拦截 llm/tool）+ run_meta（业务事件）
      - 消息通道独立，Agent 每轮回复立即落 evolve_messages，前端按 seq 增量拉
      - 无 token 流：前端轮询频率可降到 2s，DB 压力大幅降低

    消息派生规则（D2）：
      - model_output 且无 tool_calls → 落 assistant 消息（开场白/对话回复）
      - model_output 且有 tool_calls → 不落文本（工具单独走 tool 消息）
      - tool_output 工具属于 {进化点, write_*, edit_source, validate_changes} → 落 tool 消息
      - 其他工具的 tool_output → 不落（只读工具不污染对话历史）
    """
    from app.evolve.agent.event_sink import EvolveEventSink
    from app.evolve.evolve_repo import EvolveMessagesRepo

    # [诊断] 进入流式循环前确认 ctx.trace_id_self
    logger.warning(
        "[诊断] _run_agent_streamed 入口: session=%s recorder=%s trace_id_self=%r",
        ctx.session_id, ctx.recorder is not None, ctx.trace_id_self,
    )

    sink = EvolveEventSink(session_id=ctx.session_id)
    agent_events = agent.astream_events(
        {"messages": [{"role": "user", "content": user_input}]},
        config=config,
        version="v2",
    )

    async for event in agent_events:
        try:
            frame_dicts = await sink.on_event_dicts(event)
        except Exception:
            logger.exception(
                "session %s: sink 转换事件异常，跳过",
                ctx.session_id,
            )
            continue

        for frame in frame_dicts:
            frame_type = frame.get("type")
            if not frame_type:
                continue
            try:
                _persist_message_from_frame(ctx, frame, EvolveMessagesRepo)
                _emit_notification_frame(ctx, frame)
            except Exception:
                # 消息持久化失败是严重问题（会让前端看不到 Agent 动作），
                # 但不能中断 agent 主流程——ERROR 日志 + 继续跑，让 round 函数的
                # 产出检查兜底（如 design_doc 未产 → failed）。
                logger.exception(
                    "session %s: 消息派生失败 frame_type=%s（不中断 agent）",
                    ctx.session_id, frame_type,
                )

    # 循环结束后写 step_stats run_meta（DD5+DD8）：观测实际跑了多少个 superstep。
    if ctx.recorder and ctx.trace_id_self and sink.max_superstep > 0:
        try:
            ctx.recorder.append_business_event(
                ctx.trace_id_self,
                tool="step_stats",
                status="done",
                max_superstep=sink.max_superstep,
            )
        except Exception:
            logger.exception(
                "session %s: 写 step_stats 失败（不影响主流程）",
                ctx.session_id,
            )


# ── 消息持久化派生（D2）──────────────────────────────────────────

# 需要落 tool 消息的工具白名单（D2）：
#   - 进化点工具：propose/update/reject（浮窗权威状态 + 对话可见）
#   - 落地工具：write_*/edit_source（落地进度，finalizing 阶段可见）
#   - 校验工具：validate_changes（落地结果）
# 其他工具（read_eval_report/read_trace/list_elements 等只读探查）不落消息，
# 避免污染对话历史。
_MESSAGE_TOOLS = frozenset({
    # 进化点工具
    "propose_evolution_point", "update_evolution_point", "reject_evolution_point",
    # 落地写工具
    "write_design_doc", "write_meta_system", "write_outline_system",
    "write_writing_system", "write_interview_system", "write_detail_outline_system",
    "write_memory_system", "write_change_log",
    "edit_source",
    # 校验工具
    "validate_changes",
})


def _persist_message_from_frame(
    ctx: "EvolveContext",
    frame: dict[str, Any],
    messages_repo: Any,
) -> None:
    """根据 sink 帧派生持久化消息到 evolve_messages（D2）。

    规则：
      - model_output 无 tool_calls → assistant 消息（开场白/对话回复）
      - tool_output 工具在白名单 → tool 消息（含 related_points 关联进化点）
      - 其他帧（model_output 有 tool_calls / tool_call / tool_error）→ 不落
    """
    frame_type = frame.get("type")

    if frame_type == "model_output":
        text = frame.get("text", "")
        tool_calls = frame.get("tool_calls") or []
        # 有工具调用 → 不落文本消息（工具单独走 tool 消息，避免重复）
        if tool_calls or not text.strip():
            return
        # 纯文本回复 → assistant 消息
        messages_repo.append(
            ctx.session_id, role="assistant", content=text,
        )
        return

    if frame_type == "tool_output":
        tool_name = frame.get("tool_name", "")
        if tool_name not in _MESSAGE_TOOLS:
            return
        output_summary = frame.get("output_summary", "")
        if not output_summary:
            return

        # 进化点工具的 related_points 关联（前端双向高亮联动用）
        related_points: list[str] | None = None
        if tool_name in {"propose_evolution_point", "update_evolution_point", "reject_evolution_point"}:
            # 从 output_summary 提取 point_id（propose 返回值含 id=xxx）
            import re
            m = re.search(r"id=([a-f0-9]+)", output_summary)
            if m:
                related_points = [m.group(1)]

        tool_events = [{
            "tool": tool_name,
            "result_excerpt": output_summary[:200],
        }]
        # 渲染为可读的 tool 消息内容
        content = f"[工具·{tool_name}] {output_summary[:300]}"
        messages_repo.append(
            ctx.session_id, role="tool", content=content,
            tool_events=tool_events, related_points=related_points,
        )
        return


def _emit_notification_frame(ctx: "EvolveContext", frame: dict[str, Any]) -> None:
    """把 sink 帧派生为通知帧走 run_meta 通道（前端 Pull 拉到后刷消息）。

    重构后的通知帧类型（D1/D3）：
      - model_output → 通知前端"有新 assistant 消息，调 loadMessages 增量拉"
      - tool_output（白名单工具）→ 通知前端"有新 tool 消息，刷 loadMessages"

    不再传完整帧内容（消息已落库，前端拉权威存储即可），只传最小信号。
    token 流（model_stream）完全移除。
    """
    if not (ctx.recorder and ctx.trace_id_self):
        # [诊断] 守卫命中：暴露为什么 message_updated 没写
        logger.warning(
            "[诊断] _emit_notification_frame 守卫 return: session=%s recorder=%s trace_id_self=%r frame_type=%s",
            ctx.session_id, ctx.recorder is not None, ctx.trace_id_self, frame.get("type"),
        )
        return

    frame_type = frame.get("type")
    if frame_type == "model_output":
        text = frame.get("text", "")
        tool_calls = frame.get("tool_calls") or []
        # 只在有实际内容要持久化时通知（与 _persist_message_from_frame 对齐）
        if tool_calls or not text.strip():
            return
        ctx.recorder.append_business_event(
            ctx.trace_id_self,
            tool="message_updated",
            status="assistant",
        )
        logger.warning(
            "[诊断] _emit_notification_frame 已写 message_updated(assistant): session=%s trace=%s",
            ctx.session_id, ctx.trace_id_self,
        )
        return

    if frame_type == "tool_output":
        tool_name = frame.get("tool_name", "")
        if tool_name not in _MESSAGE_TOOLS:
            return
        ctx.recorder.append_business_event(
            ctx.trace_id_self,
            tool="message_updated",
            status="tool",
            tool_name=tool_name,
        )
        logger.warning(
            "[诊断] _emit_notification_frame 已写 message_updated(tool): session=%s trace=%s tool=%s",
            ctx.session_id, ctx.trace_id_self, tool_name,
        )
        return


# 不设总超时护栏（asyncio.wait_for）——进化时长不设上限，让它自然跑完。
# recursion_limit 显式设 200（避免 LangChain 框架默认 25 误杀正常进化）。
# GraphRecursionError 分支是兜底：模型陷入死循环（反复调工具不收尾）时强制收敛。


async def _handle_recursion_error(ctx: "EvolveContext", round_name: str) -> None:
    """GraphRecursionError 兜底（DD7）：标 session failed + recorder 收尾 + emit_log。

    各 round 的 ``except GraphRecursionError`` 分支调用本函数完成副作用收敛，
    然后各自 ``return`` 对应的状态 dict——helper 只管副作用，round 管返回值。
    语义：recursion error 属于异常（Agent 没收敛），归 failed；
    cancelled 只属于用户主动 stop，不与此混淆。

    Args:
        ctx:        进化上下文
        round_name: 哪个 round 触顶（inspect/converse/finalize/evolve），用于日志定位
    """
    from app.evolve import db as ev_db

    logger.warning(
        "session %s: %s round 步数触顶（recursion_limit=200），未收敛",
        ctx.session_id, round_name,
    )
    ev_db.update_session(ctx.session_id, status="failed")
    if ctx.recorder and ctx.trace_id_self:
        ctx.recorder.fail_run(
            ctx.trace_id_self, f"{round_name} round 步数触顶（未收敛）"
        )
    ctx.emit_log(
        f"{round_name} 阶段消耗过多步数仍未完成"
        "（可能反复调用工具未收尾）。请重试，或检查模型是否稳定。"
    )


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
        trajectories_summary=_format_similar_trajectories(ctx),
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

    _start_evolution_trace(ctx, "evolve-agent.run")

    # ── 阶段 1：inspect round（探查 + 设计 + 落地，单体兼容模式）──
    # 单体模式下 status 保持 running，FlowGuard 不做阶段门控（conversing 才拦），
    # Agent 一气呵成跑完探查→设计→落地→产出。
    agent = await build_evolve_agent(ctx)

    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
        "recursion_limit": 200,  # 显式放开（避免 LangChain 框架默认 25 误杀）
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
        # 步数触顶（recursion_limit=200）：模型陷入死循环没收敛（反复调工具不收尾）。
        await _handle_recursion_error(ctx, "evolve")
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

    _start_evolution_trace(ctx, "evolve-agent.inspect")

    agent = await build_evolve_agent(ctx)
    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
        "recursion_limit": 200,  # 显式放开（避免 LangChain 框架默认 25 误杀）
    }
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    ctx.emit_phase("running")
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
        await _run_agent_streamed(agent, user_input, config, ctx)
        # 探查完成，转 conversing 等用户对话
        ctx.session_status = STATUS_CONVERSING
        ev_db.update_session(ctx.session_id, status=STATUS_CONVERSING)
        ctx.emit_phase("conversing")
        ctx.emit_log("探查阶段完成，进入对话共创阶段。")
        logger.info("session %s: inspect round 完成，转 conversing", ctx.session_id)
        return {"status": "conversing", "session_id": ctx.session_id}

    except GraphRecursionError:
        # 步数触顶（recursion_limit=200）：探查阶段陷入死循环。
        await _handle_recursion_error(ctx, "inspect")
        return {
            "status": "failed", "session_id": ctx.session_id,
            "error": "inspect round 步数触顶（未收敛）",
        }
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
        "recursion_limit": 200,  # 显式放开（避免 LangChain 框架默认 25 误杀）
    }
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    logger.info("session %s: converse round 启动", ctx.session_id)
    ctx.emit_log("用户消息触发对话 round。")

    try:
        await _run_agent_streamed(agent, user_message, config, ctx)
        logger.info("session %s: converse round 完成", ctx.session_id)
        return {"status": "conversing", "session_id": ctx.session_id}

    except GraphRecursionError:
        # 步数触顶（recursion_limit=200）：对话轮陷入死循环（异常 failed，
        # 与用户主动 stop 的 cancelled 语义区分开——recursion 是 Agent 自身没收敛）。
        await _handle_recursion_error(ctx, "converse")
        return {
            "status": "failed", "session_id": ctx.session_id,
            "error": "converse round 步数触顶（未收敛）",
        }
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
    ctx.emit_phase("finalizing")
    ctx.emit_log("进入落地阶段，按已拍板的进化点开始改代码。")

    agent = await build_evolve_agent(ctx)
    config: dict[str, Any] = {
        "configurable": {"thread_id": ctx.thread_id},
        "recursion_limit": 200,  # 显式放开（避免 LangChain 框架默认 25 误杀）
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
        await _run_agent_streamed(agent, user_input, config, ctx)

        # 产出检查
        if ctx.change_log_path:
            ctx.session_status = STATUS_PENDING_REVIEW
            ev_db.update_session(ctx.session_id, status=STATUS_PENDING_REVIEW)
            ctx.emit_phase("pending_review")
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

    except GraphRecursionError:
        # 步数触顶（recursion_limit=200）：落地阶段陷入死循环。
        await _handle_recursion_error(ctx, "finalize")
        return {
            "status": "failed", "session_id": ctx.session_id,
            "error": "finalize round 步数触顶（未收敛）",
        }
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


# embedding 调用缓存（同一运行内同一查询文本复用，AC-39）。
# key = query 文本的 md5，value = embedding 向量。进程级，查询幂等故无 session 隔离必要。
_embedding_cache: dict[str, list[float]] = {}


def _format_similar_trajectories(ctx: EvolveContext) -> str:
    """相似历史问题轨迹注入（问题知识库一期，REQ-04.9 / DEC-18 / AC-06/30/31）。

    流程：
      1. 冻结当前问题卡（独立分析锚点，DEC-15）
      2. 按 problem_group 顺序检索相似标准问题（每组最多 5 个，REQ-04.2/AC-23）
      3. 深挖≤3 条事实轨迹（REQ-04.5）
      4. 格式化为文本：标注确认状态/匹配依据/效果验证阶段

    约束（AC-30/31）：只陈述事实，不生成经验对象/等级/推荐。
    降级（AC-26）：检索失败返回降级说明，不阻塞进化，不表述为"无历史问题"。
    embedding 复用（AC-39）：同一查询文本复用缓存向量，每组最多 1 次 embedding。
    """
    try:
        from app.problem_kb import current_card, repo as pk_repo
        from app.problem_kb.retrieval import search as pk_search
        from app.problem_kb.retrieval.embedder import get_embedder
        from app.problem_kb.retrieval import store as pk_store
    except ImportError:
        return ""

    dossier = ctx.eval_dossier
    if not dossier or not dossier.get("dossier_id"):
        return ""

    # 1. 冻结当前问题卡（检索前，DEC-15）
    cards = current_card.freeze_current_cards(ctx.session_id, dossier)
    if not cards:
        # 无 findings 或冻结失败——标准问题库为空时也无从检索
        return _format_trajectory_empty_note()

    # 2. 按 problem_group 聚合，逐组检索
    groups: dict[str, list[dict]] = {}
    for card in cards:
        groups.setdefault(card["problem_group"], []).append(card)

    embedder = get_embedder()
    vec_available = pk_store.is_vector_available()

    sections: list[str] = []
    any_degraded = False
    for group_key, group_cards in groups.items():
        # 用该组首张卡的代表陈述作为查询（同组问题同根因）
        rep_snapshot = group_cards[0]["frozen_snapshot"]
        query_text = rep_snapshot.get("statement", "")
        classification = rep_snapshot.get("classification", {})

        # 3. embedding（每组最多 1 次，缓存复用，AC-39）
        query_vec = None
        if embedder is not None and vec_available and query_text:
            cache_key = _embed_cache_key(query_text)
            if cache_key in _embedding_cache:
                query_vec = _embedding_cache[cache_key]
            else:
                try:
                    query_vec = embedder.embed_one(query_text)
                    _embedding_cache[cache_key] = query_vec
                except Exception:
                    query_vec = None  # embedding 失败，降级到 FTS+结构化

        # 4. 检索
        result = pk_search.search_similar_problems(
            query_text=query_text,
            query_vec=query_vec,
            classification=classification,
            top_k=5,
        )
        if result.degraded:
            any_degraded = True

        section = _format_one_group(group_key, group_cards, result)
        if section:
            sections.append(section)

    # 5. 拼装
    if not sections:
        return _format_trajectory_empty_note(degraded=any_degraded)

    header = "（按问题组召回相似历史标准问题，仅作事实参考）"
    if any_degraded:
        header += "\n注意：部分检索已降级（向量或全文索引不可用），结果可能不完整。"
    return header + "\n\n" + "\n\n".join(sections)


def _embed_cache_key(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _format_one_group(
    group_key: str,
    cards: list[dict],
    result: "object",
) -> str:
    """格式化一个问题组的检索结果（含≤3条事实轨迹深挖，REQ-04.5）。"""
    mechanism, _, nature = group_key.partition("#")
    lines = [f"### 问题组：{mechanism} / {nature}"]
    lines.append(f"当前问题（{len(cards)} 张卡）：")
    for c in cards[:3]:
        snap = c["frozen_snapshot"]
        lines.append(
            f"  - [{snap.get('severity', '?')}] {snap.get('statement', '')[:80]}"
            f"（根因假设：{snap.get('root_cause_hypothesis', '')[:50]}，"
            f"置信度{snap.get('root_cause_confidence', 0):.1f}）"
        )
    if len(cards) > 3:
        lines.append(f"  - …（另 {len(cards) - 3} 张）")

    if result.empty:  # type: ignore[attr-defined]
        lines.append("  相似历史标准问题：无（该问题组可能是新问题）")
        return "\n".join(lines)

    lines.append("相似历史标准问题（Top 5，深挖≤3 条事实轨迹）：")
    from app.problem_kb import repo as pk_repo
    for i, hit in enumerate(result.hits[:5], 1):  # type: ignore[attr-defined]
        lines.append(
            f"  {i}. 【{hit.get('confirmation_status', '?')}】{hit.get('title', '?')}"
            f"（效果验证：{hit.get('effect_stage', '?')}，"
            f"已确认实例 {hit.get('evidence_count', 0)}，"
            f"匹配：{hit.get('match_basis', '')[:60]}）"
        )
        # 深挖≤3 条事实轨迹（REQ-04.5）：取该标准问题的已确认实例 + 进化点
        if i <= 3:
            trajectory = _dig_trajectory(hit["problem_id"])
            if trajectory:
                lines.append(f"     事实轨迹：{trajectory}")

    # 增加检索命中计数（利用率统计，REQ-01.5）
    for hit in result.hits[:5]:  # type: ignore[attr-defined]
        try:
            pk_repo.increment_retrieval(hit["problem_id"])
        except Exception:
            pass
    return "\n".join(lines)


def _dig_trajectory(problem_id: str) -> str:
    """深挖一条标准问题的事实轨迹（REQ-04.5/REQ-10）。

    事实链：问题实例 → 进化点（含备选/决策）→ 效果状态。
    只陈述事实，不生成经验（AC-30/31）。
    """
    from app.problem_kb import repo as pk_repo
    parts: list[str] = []
    # 已确认实例摘要（最多 2 条）
    links = pk_repo.list_links_for_problem(problem_id)
    if links:
        import app.core.db as _db
        instance_ids = [l["instance_id"] for l in links[:2]]
        placeholders = ",".join("?" * len(instance_ids))
        instances = _db.query_all(
            f"SELECT statement, severity, created_at FROM problem_instances "
            f"WHERE instance_id IN ({placeholders})",
            tuple(instance_ids),
        )
        for inst in instances:
            parts.append(
                f"实例[{inst['severity']}] {inst['statement'][:40]}"
            )
    # 进化点归属（含 proposed/accepted/rejected 全态）
    ownerships = pk_repo.list_ownership_for_problem(problem_id)
    if ownerships:
        import app.core.db as _db
        point_ids = [o["point_id"] for o in ownerships[:2]]
        placeholders = ",".join("?" * len(point_ids))
        points = _db.query_all(
            f"SELECT target, status, problem FROM evolve_points "
            f"WHERE id IN ({placeholders})",
            tuple(point_ids),
        )
        for p in points:
            parts.append(
                f"进化点[{p['status']}] 改 {p['target'][:30]}"
            )
    return "；".join(parts) if parts else "（无已确认事实轨迹）"


def _format_trajectory_empty_note(degraded: bool = False) -> str:
    """检索无结果时的说明（AC-26：不得表述为"无历史问题"）。"""
    if degraded:
        return "（知识检索不可用：向量或全文索引降级，本次未注入历史轨迹）"
    return "（问题知识库暂无相似历史标准问题；本次问题可能为新问题）"


__all__ = [
    "build_evolve_agent",
    "run_evolve_session",
    "run_inspect_round",
    "run_converse_round",
    "run_finalize_round",
]
