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

import logging
from typing import Any

from deepagents import create_deep_agent

from app.eval_agent.ctx import EvaluationContext, set_eval_context
from app.eval_agent.middleware.no_fs import NoFilesystemToolsMiddleware
from app.eval_agent.prompt import EVAL_SYSTEM_PROMPT
from app.eval_agent.tools import clear_content_tasks, make_eval_tools
from app.common.model_factory import build_agent_model

logger = logging.getLogger("evolution.eval_agent.agent")


def build_eval_agent(ctx: EvaluationContext):
    """构建评估 Agent（DeepAgent 顶层）。

    Args:
        ctx: 本次评估流程的上下文（注入到工具闭包）

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
        f"- 被评估的 trace_id: {ctx.trace_id}\n"
    )
    if ctx.agent_version_type:
        system_prompt += f"- 该 trace 对应的 Agent 版本: {ctx.agent_version_type}"
        if ctx.agent_version_id is not None:
            system_prompt += f" v{ctx.agent_version_id}"
        system_prompt += "\n"

    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=[NoFilesystemToolsMiddleware()],  # V4：过滤 read_file 等 filesystem 工具
        subagents=None,
        checkpointer=None,
    )
    logger.info(
        "评估 Agent 构建完成: eval_id=%s trace=%s",
        ctx.eval_id, ctx.trace_id,
    )
    return agent


async def run_eval_session(ctx: EvaluationContext) -> dict[str, Any]:
    """跑一次完整的评估 session。

    Args:
        ctx: 评估上下文（已绑定 trace_id）

    Returns:
        {"status": "done"|"failed", "eval_id": ...}
    """
    agent = build_eval_agent(ctx)

    ctx.emit_log("评估 Agent 启动，开始诊断评估...")
    logger.info("eval %s: 评估 Agent 启动 (trace=%s)", ctx.eval_id, ctx.trace_id)

    user_input = (
        f"请评估 trace_id={ctx.trace_id} 的执行质量。"
        "按 system prompt 的步骤：看流程硬指标 → 读 trace 摘要 → "
        "深挖关键节点 → 读设计意图 → 取内容分数 → 产出诊断报告。"
        "记住只诊断不提方案。"
    )

    try:
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"recursion_limit": 80},
        )
        logger.info("eval %s: 评估 Agent 执行完成", ctx.eval_id)

        # 清理后台内容评估任务引用
        clear_content_tasks()

        return {"status": "done", "eval_id": ctx.eval_id}

    except Exception as e:
        logger.exception("eval %s: 评估 Agent 执行失败", ctx.eval_id)
        clear_content_tasks()
        return {"status": "failed", "eval_id": ctx.eval_id, "error": str(e)}


__all__ = ["build_eval_agent", "run_eval_session"]
