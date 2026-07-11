"""评估 Agent 构建（决策 S1）。

用 DeepAgent 框架（create_deep_agent）构建独立顶层评估 Agent（不再是驱动器的子代理）：
  - model: 复用 build_agent_model（judge 配置，common/model_factory）
  - tools: 评估专属工具集（make_eval_tools，读 EvaluationContext）
  - system_prompt: EVAL_SYSTEM_PROMPT（只诊断不提方案，S14/T4）
  - middleware: 无（评估 Agent 自主跑，无阶段约束）
  - subagents: 无（评估不分委托，自己一把手）

与执行端 MetaAgent 同构——都是 create_deep_agent 顶层 Agent。

工作流：评估 Agent 拿到 trace_id 后自主跑（read_trace → read_surface →
get_content_score → write_eval_report），产出写入 evaluation_sessions 表（S2 DB 交接）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from deepagents import create_deep_agent
from langgraph.errors import GraphRecursionError

from app.eval_agent.ctx import EvaluationContext, set_eval_context
from app.eval_agent.middleware.no_fs import NoFilesystemToolsMiddleware
from app.eval_agent.prompt import EVAL_SYSTEM_PROMPT
from app.eval_agent.tools import clear_content_tasks, make_eval_tools
from app.common.model_factory import build_agent_model
from app.trace import TraceMiddleware, TraceCallbackHandler

logger = logging.getLogger("evolution.eval_agent.agent")

# 评估 Agent 的安全护栏（防止一次评估无限期挂起）：
#   - EVAL_TOTAL_TIMEOUT: 整次评估的总超时（含 model + tool 全流程），主时间护栏。
#
# 不设 recursion_limit 步数限制：评估全流程（读 trace/读 surface/取内容分数/写报告）
# 的时长完全由 EVAL_TOTAL_TIMEOUT 兜底，步数交给框架默认（≈10007，事实上的不限制），
# 避免正常评估因步数上限被误杀。GraphRecursionError 分支仅作极端死循环的防御性兜底。
EVAL_TOTAL_TIMEOUT = 300  # 秒：一次评估最多 5 分钟


def build_eval_agent(ctx: EvaluationContext):
    """构建评估 Agent（DeepAgent 顶层 + TraceMiddleware 注入，D6/D10）。

    ctx.trace_id_self 必须已由 create_run 设置（run_eval_session 先调 create_run）。
    TraceMiddleware 只挂顶层一个（D10），子代理不挂（评估 Agent 无子代理）。

    Args:
        ctx: 本次评估流程的上下文（注入到工具闭包，含 recorder + trace_id_self）

    Returns:
        编译后的 CompiledStateGraph（可 ainvoke）
    """
    # 注入工具上下文（评估专属 contextvar，与进化 ctx 独立）
    set_eval_context(ctx)

    # model（复用 judge 配置）
    model = build_agent_model(temperature=0.2)

    # 评估专属工具
    tools = make_eval_tools()

    # system prompt 带上 case 信息
    system_prompt = (
        f"{EVAL_SYSTEM_PROMPT}\n\n"
        f"## 当前评估 session\n"
        f"- eval_id: {ctx.eval_id}\n"
        f"- 被评估的 trace_id: {ctx.input_trace_id}\n"
    )
    if ctx.agent_version_type:
        system_prompt += f"- 该 trace 对应的 Agent 版本: {ctx.agent_version_type}"
        if ctx.agent_version_id is not None:
            system_prompt += f" v{ctx.agent_version_id}"
        system_prompt += "\n"

    # D6/D10：TraceMiddleware 注入（recorder + trace_id_self + agent_name）。
    trace_middleware = TraceMiddleware(
        recorder=ctx.recorder,
        trace_id=ctx.trace_id_self,
        agent_name="eval-agent",
    ) if ctx.recorder and ctx.trace_id_self else None

    middleware_list = [NoFilesystemToolsMiddleware()]  # V4：过滤 read_file 等 filesystem 工具
    if trace_middleware:
        middleware_list.append(trace_middleware)

    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware_list,
        subagents=None,
        checkpointer=None,
    )
    logger.info(
        "评估 Agent 构建完成: eval_id=%s trace=%s",
        ctx.eval_id, ctx.input_trace_id,
    )
    return agent


async def run_eval_session(ctx: EvaluationContext) -> dict[str, Any]:
    """跑一次完整的评估 session。

    Args:
        ctx: 评估上下文（已绑定 input_trace_id + recorder）

    Returns:
        {"status": "done"|"failed", "eval_id": ...}
    """
    # D6/D8：先 create_run 拿自观测 trace_id，再构建 agent（middleware 需要 trace_id）。
    if ctx.recorder:
        handle = ctx.recorder.create_run(
            session_id=ctx.eval_id,
            run_purpose="evolution_eval",
            endpoint="eval-agent.run",
        )
        ctx.trace_id_self = handle.trace_id

    agent = build_eval_agent(ctx)

    # D6：config 注入 TraceCallbackHandler（构建调用树）。
    # 不设 recursion_limit——步数交给框架默认（事实上的不限制），时长靠
    # EVAL_TOTAL_TIMEOUT 兜底（见下方 asyncio.wait_for）。
    config: dict[str, Any] = {}
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    ctx.emit_log("评估 Agent 启动，开始诊断评估...")
    logger.info("eval %s: 评估 Agent 启动 (input_trace=%s)", ctx.eval_id, ctx.input_trace_id)

    user_input = (
        f"请评估 trace_id={ctx.input_trace_id} 的执行质量。"
        "按 system prompt 的步骤：看流程硬指标 → 读 trace 摘要 → "
        "深挖关键节点 → 读设计意图 → 取内容分数 → 产出诊断报告。"
        "记住只诊断不提方案。"
    )

    try:
        try:
            await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config=config,
                ),
                timeout=EVAL_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "eval %s: 评估 Agent 总超时（%ds），强制结束",
                ctx.eval_id, EVAL_TOTAL_TIMEOUT,
            )
            ctx.emit_log(f"评估总耗时超过 {EVAL_TOTAL_TIMEOUT}s 上限，已强制结束。")
            clear_content_tasks()
            if ctx.recorder and ctx.trace_id_self:
                ctx.recorder.fail_run(
                    ctx.trace_id_self, f"评估总超时（{EVAL_TOTAL_TIMEOUT}s）"
                )
            return {
                "status": "failed", "eval_id": ctx.eval_id,
                "error": f"评估总超时（{EVAL_TOTAL_TIMEOUT}s）",
            }

        logger.info("eval %s: 评估 Agent 执行完成", ctx.eval_id)

        # 清理后台内容评估任务引用
        clear_content_tasks()

        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.complete_run(ctx.trace_id_self)

        return {"status": "done", "eval_id": ctx.eval_id}

    except GraphRecursionError:
        # 步数触顶（仅当框架默认上限被触达时出现，正常情况下不会到这）：
        # 模型陷入死循环没收敛（反复调工具不收尾）。
        # 不是程序错误，是 Agent 行为问题——给清晰提示而非英文 traceback。
        logger.warning("eval %s: 评估 Agent 步数触顶（框架默认上限），未收敛", ctx.eval_id)
        ctx.emit_log(
            "评估 Agent 消耗了过多步数仍未完成诊断"
            "（可能反复查阅信息未收尾）。请重试，或检查模型是否稳定。"
        )
        clear_content_tasks()
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(
                ctx.trace_id_self,
                "评估 Agent 步数触顶（未收敛）",
            )
        return {
            "status": "failed", "eval_id": ctx.eval_id,
            "error": "评估 Agent 步数触顶（未收敛）",
        }
    except Exception as e:
        logger.exception("eval %s: 评估 Agent 执行失败", ctx.eval_id)
        clear_content_tasks()
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(ctx.trace_id_self, e)
        return {"status": "failed", "eval_id": ctx.eval_id, "error": str(e)}
        return {"status": "failed", "eval_id": ctx.eval_id, "error": str(e)}


__all__ = ["build_eval_agent", "run_eval_session"]
