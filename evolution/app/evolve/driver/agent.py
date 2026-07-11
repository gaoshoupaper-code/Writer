"""进化 Agent 装配（三功能解耦，决策 S3/T9）。

精简为「方案→执行」两阶段驱动器。删除一把手自主模式（run_evolve_session）
和 6 阶段流水线（run_evolve_pipeline）——评估已独立成 Agent，自证比分
机制废弃，进化只负责吃评估报告产改动。

架构（S3）：
  create_deep_agent(
      tools=[task(框架自带)],
      subagents=[plan, execute],       # D2: SubAgentSpec 挂载
      middleware=[PhaseGuardMiddleware()],  # S3: 2阶段白名单
  )

驱动器自身只持 task（委托工具），不做分析/写代码：
  - plan 阶段：委托 plan 子代理（读评估报告 + trace → 产方案）
  - execute 阶段：委托 execute 子代理（按方案改源码 + 校验 → 产 change_log）

输入：trace_id + 评估报告（从 evaluation_sessions 表加载到 ctx.eval_snapshot，S2）
产出：harnesses/current/ 代码改动 + change_log → 待审（pending_review）
发版：人工动作（Phase 4）
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphRecursionError

from app.core.settings import settings
from app.common.model_factory import build_agent_model
from app.evolve.ctx import EvolveContext, set_tool_context
from app.evolve.driver.prompt import driver_system_prompt
from app.evolve.driver.middleware.phase_guard import PhaseGuardMiddleware, TERMINAL_PHASE
from app.evolve.subagents.execute.build import build_execute_subagent
from app.evolve.subagents.plan.build import build_plan_subagent
from app.trace import TraceMiddleware, TraceCallbackHandler

logger = logging.getLogger("evolution.evolve.agent")

# 进化驱动器的安全护栏（防止一次进化无限期挂起）：
#   - EVOLVE_TOTAL_TIMEOUT: 整次进化的总超时（含 plan + execute 全流程），主时间护栏。
#
# 不设 recursion_limit 步数限制：进化全流程的时长完全由 EVOLVE_TOTAL_TIMEOUT 兜底，
# 步数交给框架默认（≈10007，事实上的不限制），避免正常进化因步数上限被误杀。
# GraphRecursionError 分支仅作极端死循环的防御性兜底。
EVOLVE_TOTAL_TIMEOUT = 1800  # 秒：一次进化最多 30 分钟


def _ensure_workspace() -> Path:
    """确保 evolve workspace 目录存在（Agent 的 edits.json 存放点）。"""
    ws = settings._evolution_root / "data" / "evolve_workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def build_evolve_driver(ctx: EvolveContext):
    """构建进化驱动器（DeepAgent + 2 子代理 + PhaseGuard）。

    架构（S3）：
      create_deep_agent(
          tools=[task(框架自带)],
          subagents=[plan, execute],
          middleware=[PhaseGuardMiddleware()],  # 2阶段白名单
      )

    驱动器只持 task 委托工具，自身不做分析/写代码。

    Args:
        ctx: 进化上下文（trace_id + eval_snapshot 已作为输入填入）

    Returns:
        编译后的 CompiledStateGraph（可 ainvoke/astream）
    """
    from deepagents import create_deep_agent

    set_tool_context(ctx)

    model = build_agent_model(temperature=0.2)

    # 2 子代理（S3：删 evaluate，评估已独立）。
    # TraceMiddleware 必须传给每个子代理各自挂一份——DeepAgents 的 middleware
    # 不从父 agent 传播到子 agent（SubAgentMiddleware._build_subagent_config 只转发
    # callbacks/tags/configurable），否则子代理内部 LLM/工具调用（read_trace /
    # apply_edits / write_file 等）不会被记录。推翻旧决策 D10「只挂顶层」。
    subagents = [
        build_plan_subagent(model, recorder=ctx.recorder, trace_id_self=ctx.trace_id_self),
        build_execute_subagent(model, recorder=ctx.recorder, trace_id_self=ctx.trace_id_self),
    ]

    system_prompt = driver_system_prompt(
        session_id=ctx.session_id,
        trace_id=ctx.trace_id,
        eval_summary=_format_eval_summary(ctx),
        reflections_summary=_format_reflections(ctx),
    )

    # D6：顶层驱动器自身的 TraceMiddleware（记录委托决策 LLM + task 工具调用）。
    trace_middleware = TraceMiddleware(
        recorder=ctx.recorder,
        trace_id=ctx.trace_id_self,
        agent_name="evolve-driver",
    ) if ctx.recorder and ctx.trace_id_self else None

    middleware_list = [PhaseGuardMiddleware()]
    if trace_middleware:
        middleware_list.append(trace_middleware)

    driver = create_deep_agent(
        model=model,
        tools=[],  # 驱动器自身无工具，靠 task 委托（task 由框架自带）
        system_prompt=system_prompt,
        middleware=middleware_list,
        subagents=subagents,
        checkpointer=None,
    )
    logger.info(
        "进化驱动器构建完成: session=%s trace=%s",
        ctx.session_id, ctx.trace_id,
    )
    return driver


def _format_eval_summary(ctx: EvolveContext) -> str:
    """把评估报告快照格式化成驱动器 system prompt 的摘要。"""
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
    """从反思库提取与当前评估问题相关的失败模式，格式化为 prompt 摘要（D19）。

    按 eval_snapshot.findings 的 dimension 查相关反思，每类取 top 3。
    无反思或查询失败返回空串（prompt 里不渲染反思段）。
    """
    try:
        from app.reflection import repo as reflection_repo
    except ImportError:
        return ""

    # 从评估 findings 提取问题分类
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
        # 无 findings 分类：取全部反思 top 5
        reflections = reflection_repo.list_all(limit=5)
    else:
        reflections = reflection_repo.list_by_categories(categories, limit_per_category=3)

    if not reflections:
        return ""

    lines = [f"（共 {len(reflections)} 条历史失败模式）"]
    for r in reflections[:10]:  # 最多渲染 10 条
        hit = r.get("hit_count", 0)
        lines.append(
            f"  • [{r['category']}] (命中{hit}次) {r['pattern'][:120]}"
        )
    return "\n".join(lines)


async def run_evolve_session(ctx: EvolveContext, trace_id: str) -> dict[str, Any]:
    """跑一次完整的进化 session（方案→执行两阶段）。

    Args:
        ctx: 进化上下文（eval_snapshot 已加载评估报告）
        trace_id: 被进化的 trace id

    Returns:
        {"status": "done"|"failed", "session_id": ...}
    """
    from app.evolve import db as ev_db

    ctx.trace_id = trace_id
    ctx.review_status = "running"
    ev_db.update_session(ctx.session_id, status="running")

    # D6/D8：先 create_run 拿自观测 trace_id，再构建 driver（middleware 需要 trace_id）。
    if ctx.recorder:
        handle = ctx.recorder.create_run(
            session_id=ctx.session_id,
            run_purpose="evolution_evolve",
            endpoint="evolve-driver.run",
        )
        ctx.trace_id_self = handle.trace_id

    driver = build_evolve_driver(ctx)

    # D6：config 注入 TraceCallbackHandler（构建调用树）。
    # 不设 recursion_limit——步数交给框架默认（事实上的不限制），时长靠
    # EVOLVE_TOTAL_TIMEOUT 兜底（见下方 asyncio.wait_for）。
    config: dict[str, Any] = {}
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    ctx.emit_log("进化驱动器启动，开始 方案→执行 两阶段流水线...")
    logger.info("session %s: 驱动器启动 trace=%s", ctx.session_id, trace_id)

    user_input = (
        f"请开始进化流程。trace_id={trace_id}，case_id={ctx.case_id}。"
        f"按 2 阶段顺序执行：先委托 plan 子代理（读评估报告 + trace，设计改进方案），"
        f"再委托 execute 子代理（按方案落地代码改动 + 校验）。"
        f"注意：评估报告已加载到上下文（read_eval_report 可读）。"
    )

    try:
        try:
            await asyncio.wait_for(
                driver.ainvoke(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config=config,
                ),
                timeout=EVOLVE_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "session %s: 驱动器总超时（%ds），强制结束",
                ctx.session_id, EVOLVE_TOTAL_TIMEOUT,
            )
            ctx.emit_log(f"进化总耗时超过 {EVOLVE_TOTAL_TIMEOUT}s 上限，已强制结束。")
            ev_db.update_session(ctx.session_id, status="failed")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.fail_run(
                    ctx.trace_id_self, f"进化总超时（{EVOLVE_TOTAL_TIMEOUT}s）"
                )
            return {
                "status": "failed", "session_id": ctx.session_id,
                "error": f"进化总超时（{EVOLVE_TOTAL_TIMEOUT}s）",
            }
        except GraphRecursionError:
            # 步数触顶（仅当框架默认上限被触达时出现，正常情况下不会到这）：
            # 模型陷入死循环没收敛（plan/execute 反复调工具不收尾）。
            logger.warning(
                "session %s: 驱动器步数触顶（框架默认上限），未收敛", ctx.session_id
            )
            ctx.emit_log(
                "进化驱动器消耗了过多步数仍未完成"
                "（可能 plan/execute 反复调用工具未收尾）。请重试，或检查模型是否稳定。"
            )
            ev_db.update_session(ctx.session_id, status="failed")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.fail_run(
                    ctx.trace_id_self, "进化驱动器步数触顶（未收敛）"
                )
            return {
                "status": "failed", "session_id": ctx.session_id,
                "error": "进化驱动器步数触顶（未收敛）",
            }

        logger.info("session %s: 驱动器执行完成", ctx.session_id)

        # 执行完成 → 转 pending_review（待审，working 区锁定）
        if ctx.change_log_path:
            ctx.review_status = "pending_review"
            ev_db.update_session(ctx.session_id, status="pending_review")
            ctx.emit_log("进化流程完成，改动已落地，等待人工 review 发版。")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.complete_run(ctx.trace_id_self)
            return {"status": "done", "session_id": ctx.session_id}
        else:
            ctx.emit_log("驱动器结束但未产出 change_log（执行阶段可能未完成）。")
            ev_db.update_session(ctx.session_id, status="failed")
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.fail_run(ctx.trace_id_self, "未产出 change_log")
            return {"status": "incomplete", "session_id": ctx.session_id}

    except Exception as e:
        logger.exception("session %s: 驱动器执行失败", ctx.session_id)
        ev_db.update_session(ctx.session_id, status="failed")
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(ctx.trace_id_self, e)
        return {"status": "failed", "error": str(e), "session_id": ctx.session_id}


__all__ = ["build_evolve_driver", "run_evolve_session"]
