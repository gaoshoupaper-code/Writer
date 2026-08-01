"""评估 Agent 构建（决策 S1）。

用 DeepAgent 框架（create_deep_agent）构建独立顶层评估 Agent（不再是驱动器的子代理）：
  - model: 复用 build_agent_model（judge 配置，common/model_factory）
  - tools: 评估专属工具集（make_eval_tools，读 EvaluationContext）
  - system_prompt: EVAL_SYSTEM_PROMPT（只诊断不提方案，S14/T4）
  - middleware: 无（评估 Agent 自主跑，无阶段约束）
  - subagents: 无（评估不分委托，自己一把手）

与执行端 MetaAgent 同构——都是 create_deep_agent 顶层 Agent。

工作流：评估 Agent 拿到 trace_id 后自主跑（read_trace →
get_content_score → write_eval_report），产出写入 evaluation_sessions 表（S2 DB 交接）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from deepagents import create_deep_agent
from langgraph.errors import GraphRecursionError

from app.eval_agent import repo as eval_repo
from app.eval_agent.ctx import EvaluationContext, set_eval_context
from app.common.middleware.no_fs import NoFilesystemToolsMiddleware
from app.eval_agent.prompt import EVAL_SYSTEM_PROMPT
from app.eval_agent.tools import clear_content_tasks, make_eval_tools
from app.common.model_factory import build_agent_model
from app.trace import TraceMiddleware, TraceCallbackHandler
from app.trace.facts import add_lineage
from contracts.trace import TraceSpanLink

logger = logging.getLogger("evolution.eval_agent.agent")

# 不设总超时护栏（asyncio.wait_for）——评估时长不设上限，让它自然跑完。
# 注意：recursion_limit 未显式设置，走 LangChain 框架默认 25（DeepAgents 想设 9999
# 但未透传到运行时）。评估流程比进化简单，目前 25 够用；若未来触顶可参照
# evolve/agent.py 的处理（显式 config["recursion_limit"] = N + _handle_recursion_error 兜底）。


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

    # model（FR-007 判评分离：eval agent 用独立 eval scope，与 evolve/writer 异家族，
    # 根治 Preference Leakage。eval scope 未配置时 build_agent_model 降级用 evolution scope）
    model = build_agent_model(temperature=0.2, scope="eval")

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


def _extract_last_ai_text(result: Any) -> str:
    """从 ainvoke 返回值提取最后一条 AI 消息的文本内容。

    DeepAgent（LangGraph）ainvoke 返回标准 state dict，含 "messages" 列表。
    取最后一条 AIMessage 的文本作为 Agent 的最终产出。
    """
    messages = None
    if isinstance(result, dict):
        messages = result.get("messages")
    if not messages:
        return ""
    # 从后往前找第一条有内容的 AI 消息
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and getattr(msg, "type", None) == "ai":
            return str(content)
    # 兜底：取最后一条有内容的消息
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content:
            return str(content)
    return ""


def _is_eval_successfully_sealed(session: dict[str, Any] | None) -> bool:
    """FR-004 评估成功的唯一判据：业务 completed 且有有效 sealed_dossier_id。

    统一完成判据（CON-002）：封存成功是唯一成功标志，由 sealer 在单事务内写入
    status='completed' + sealed_dossier_id。这里只认 completed（不认旧 done 字面量），
    消除 Agent/sealer/API 三层对成功状态词的分裂。
    """
    if not session:
        return False
    return (
        session.get("status") == "completed"
        and bool(session.get("sealed_dossier_id"))
    )


def _fallback_report(ctx: EvaluationContext, result: Any) -> None:
    """降级兜底：Agent 正常结束但没调 write_eval_report（未封存评估卷宗）。

    阶段 C 语义：评估成功的唯一标志是封存了评估卷宗（completed + sealed_dossier_id）。
    Agent 漏调报告工具 = 未封存卷宗 = 评估失败（无法解锁进化）。
    这里把 Agent 最后的输出存为 failure_reason 供诊断，但状态标 failed。
    """
    ai_text = _extract_last_ai_text(result)
    fallback_reason = (
        "评估 Agent 未调用 write_eval_report，未封存评估卷宗。"
        f"Agent 最后输出：{(ai_text or '（无可提取内容）')[:500]}"
    )
    try:
        eval_repo.update_session(
            ctx.eval_id,
            status="failed",
            failure_reason=fallback_reason,
        )
        ctx.emit_log("评估 Agent 未调用报告工具，本次评估未封存卷宗（failed）。")
        logger.warning(
            "eval %s: Agent 未调 write_eval_report，标 failed（未封存卷宗）", ctx.eval_id,
        )
    except Exception:
        logger.exception("eval %s: 降级处理失败", ctx.eval_id)


async def run_eval_session(ctx: EvaluationContext) -> dict[str, Any]:
    """跑一次完整的评估 session。

    Args:
        ctx: 评估上下文（已绑定 input_trace_id + recorder）

    Returns:
        {"status": "done"|"failed", "eval_id": ...}
    """
    # D6/D8：先 create_run 拿自观测 trace_id，再构建 agent（middleware 需要 trace_id）。
    if ctx.recorder:
        compile_trace_id = str((ctx.dossier or {}).get("compile_trace_id") or "")
        handle = ctx.recorder.create_run(
            session_id=ctx.eval_id,
            run_purpose="evolution_eval",
            endpoint="eval-agent.run",
            session_type="eval",  # trace 稳定性重构：持久化 self_trace_id
            workload="evaluation",
            links=[TraceSpanLink(
                target_trace_id=compile_trace_id,
                relation="consumes",
                artifact={"type": "evidence_dossier", "id": ctx.dossier_id},
                attributes={"dossier_version": ctx.dossier_version},
            )],
            external_refs={
                "evaluation_id": ctx.eval_id,
                "dossier_id": str(ctx.dossier_id),
                "source_trace_id": ctx.input_trace_id,
            },
        )
        ctx.trace_id_self = handle.trace_id
        try:
            add_lineage(
                "trace", ctx.trace_id_self, "consumes",
                "evidence_dossier", str(ctx.dossier_id),
            )
        except Exception as exc:
            ctx.recorder.fail_run(ctx.trace_id_self, exc)
            raise

    agent = build_eval_agent(ctx)

    # D6：config 注入 TraceCallbackHandler（构建调用树）。
    # 不设 recursion_limit——步数交给框架默认（事实上的不限制），也不设总超时。
    config: dict[str, Any] = {}
    if ctx.recorder and ctx.trace_id_self:
        config["callbacks"] = [TraceCallbackHandler(ctx.recorder, ctx.trace_id_self)]

    ctx.emit_log("评估 Agent 启动，开始诊断评估...")
    logger.info("eval %s: 评估 Agent 启动 (input_trace=%s)", ctx.eval_id, ctx.input_trace_id)

    user_input = (
        f"请评估 trace_id={ctx.input_trace_id} 的执行质量。"
        "按 system prompt 的步骤：看流程硬指标 → 读 trace 摘要 → "
        "深挖关键节点 → 取内容分数 → 产出诊断报告。"
        "记住只诊断不提方案。"
    )

    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
        )

        logger.info("eval %s: 评估 Agent 执行完成", ctx.eval_id)

        # 清理后台内容评估任务引用
        clear_content_tasks(ctx.eval_id)

        # FR-004 / CON-002：评估成功的唯一判据是「封存成功」——sealer 在封存成功时
        # 已把 evaluation_sessions.status 写为 completed 并回填 sealed_dossier_id。
        # Agent 正常结束但若没调 write_eval_report（status 仍 running），视为失败。
        # 关键：判据统一为 completed（不再检查旧的 done 字面量——那是状态迁移未闭合
        # 制造的 bug：封存成功写 completed，收尾却只认 done，于是把已封存的评估改回 failed）。
        session = eval_repo.get_session(ctx.eval_id)
        if session and not _is_eval_successfully_sealed(session):
            _fallback_report(ctx, result)

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
        clear_content_tasks(ctx.eval_id)
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(
                ctx.trace_id_self,
                "评估 Agent 步数触顶（未收敛）",
            )
        return {
            "status": "failed", "eval_id": ctx.eval_id,
            "error": "评估 Agent 步数触顶（未收敛）",
        }
    except asyncio.CancelledError:
        # 用户手动停止（stop 端点调 task.cancel）：ainvoke 在某个 await 点被中断。
        # 推进 cancelled 终态 + recorder 收尾。不 re-raise——否则会被 _run_eval_bg
        # 的 except Exception 当失败处理，覆盖刚标的 cancelled。
        logger.info("eval %s: 评估 Agent 被用户停止", ctx.eval_id)
        ctx.emit_log("评估已被手动停止。")
        clear_content_tasks(ctx.eval_id)
        eval_repo.update_session(ctx.eval_id, status="cancelled")
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.cancel_run(ctx.trace_id_self, reason="user_stop")
        return {"status": "cancelled", "eval_id": ctx.eval_id}
    except Exception as e:
        logger.exception("eval %s: 评估 Agent 执行失败", ctx.eval_id)
        clear_content_tasks(ctx.eval_id)
        if ctx.recorder and ctx.trace_id_self:
            ctx.recorder.fail_run(ctx.trace_id_self, e)
        return {"status": "failed", "eval_id": ctx.eval_id, "error": str(e)}


__all__ = ["build_eval_agent", "run_eval_session"]
