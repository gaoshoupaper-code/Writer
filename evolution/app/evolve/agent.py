"""进化 Agent 装配（核心）。

用 DeepAgent 框架（create_deep_agent）构建进化 Agent：
  - model: ChatOpenAI（复用 judge 配置，agent_model.build_evolve_model）
  - tools: 6 个领域工具（tools._make_tools）+ 框架自带的 read_file/write_file/...
  - middleware: EvolutionGuardMiddleware（闭环护栏）
  - system_prompt: EVOLVE_SYSTEM_PROMPT

Agent 全流程一把手：自主决定调哪个工具，跑通完整闭环。

工作目录：evolution/data/evolve_workspace/（Agent 的 execute 工具的 cwd，
也是 edits.json 的存放点）。Agent 通过 write_file/edit_file 改
evolution/harnesses/current/ 下的源码（框架自带工具，cwd 设为项目根）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from app.core.settings import settings
from app.evolve.agent_model import build_evolve_model
from app.evolve.guard import EvolutionGuardMiddleware
from app.evolve.prompt import EVOLVE_SYSTEM_PROMPT
from app.evolve.tools import EvolveContext, set_tool_context, _make_tools

logger = logging.getLogger("evolution.evolve.agent")


def _ensure_workspace() -> Path:
    """确保 evolve workspace 目录存在（Agent 的 edits.json 存放点）。"""
    ws = settings._evolution_root / "data" / "evolve_workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def build_evolve_agent(ctx: EvolveContext):
    """构建进化 Agent（DeepAgent）。

    Args:
        ctx: 本次进化流程的上下文（注入到工具闭包）

    Returns:
        编译后的 CompiledStateGraph（可 ainvoke/astream）
    """
    # 注入工具上下文（工具闭包捕获 ctx_global）
    set_tool_context(ctx)

    # 准备 workspace
    ws = _ensure_workspace()

    # model（复用 judge 配置）
    model = build_evolve_model(temperature=0.2)

    # 工具：领域工具 + 框架自带文件工具（create_deep_agent 默认带 read/write_file 等）
    custom_tools = _make_tools()

    # system prompt 带上 case 信息
    system_prompt = (
        f"{EVOLVE_SYSTEM_PROMPT}\n\n"
        f"## 当前 session\n- session_id: {ctx.session_id}\n"
        f"- 评估 case: {ctx.case_id}\n"
        f"- 你的工作目录: {ws}\n"
        f"- harness 包根: {settings.harness_work_dir_path}\n"
    )

    agent = create_deep_agent(
        model=model,
        tools=custom_tools,
        system_prompt=system_prompt,
        middleware=[EvolutionGuardMiddleware()],
        # 不需要 subagents（进化 Agent 自己就是一把手，不分委托）
        # 不挂 GENERAL_PURPOSE_SUBAGENT（进化不需要通用子代理）
        subagents=None,
        # checkpointer=None：单轮同步执行，不需要中断恢复
        checkpointer=None,
    )
    logger.info("进化 Agent 构建完成: session=%s case=%s", ctx.session_id, ctx.case_id)
    return agent


async def run_evolve_session(ctx: EvolveContext, user_input: str) -> dict[str, Any]:
    """跑一次完整的进化 session。

    Args:
        ctx: 进化上下文
        user_input: 给 Agent 的初始指令（如"请开始进化 case-001"）

    Returns:
        Agent 最终状态 + report
    """
    agent = build_evolve_agent(ctx)

    ctx.emit_log("进化 Agent 启动，开始自主进化流程...")
    logger.info("session %s: 进化 Agent 启动", ctx.session_id)

    try:
        # ainvoke 跑完整流程（Agent 自主决定调哪些工具）
        # DeepAgent 默认 max_turns 可能不够，进化流程步骤多，设大一些
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"recursion_limit": 100},
        )
        logger.info("session %s: 进化 Agent 执行完成", ctx.session_id)

        # 检查是否产出 report
        if ctx.report:
            ctx.emit_log("进化流程完成，已产出报告。")
            return {"status": "done", "report": ctx.report}
        else:
            ctx.emit_log("进化流程结束但未产出完整报告。")
            return {"status": "incomplete", "report": None}

    except Exception as e:
        logger.exception("session %s: 进化 Agent 执行失败", ctx.session_id)
        ctx.events.finish("failed", str(e)) if ctx.events else None
        return {"status": "failed", "error": str(e), "report": None}


__all__ = ["build_evolve_agent", "run_evolve_session"]
