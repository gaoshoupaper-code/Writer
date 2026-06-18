"""访谈子代理 — 需求访谈，产出 demand.md（方式2 HITL）。

通过 ask_user 工具（interrupt）与用户多轮问答，按 demand 模板逐项填充 demand.md；
维度齐全后请求用户确认，confirmed 后交回 MetaAgent。
不挂 evolution（需求访谈无客观质量标准）。多轮 interrupt 冒泡由 P0 spike 验证。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deepagents import CompiledSubAgent, create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from app.platform.agent.middleware import ErrorRecoveryMiddleware, FilesystemPathGuardMiddleware
from app.platform.tools import build_ask_user_tool

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "interview_system.md"


def build_interview_deep_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: object,
    middleware_factory: Callable[[str], list[AgentMiddleware]],
) -> CompiledSubAgent:
    """构建访谈子代理（DeepAgent，无 evolution，方式2 HITL）。

    Args:
        workspace_root:     工作区根目录
        model:              聊天模型
        backend:            DeepAgents 后端（文件系统）
        middleware_factory: 中间件工厂（按 agent_name 生成通用中间件：ErrorRecovery/Trace/PathGuard）
    """
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()

    # ErrorRecovery 会把 ask_user 的 interrupt() 当工具错误重试耗尽，破坏 HITL，须移除；
    # PathGuard 换成下方 demand.md 限定（只允许写 demand.md）。
    middleware = [
        m for m in middleware_factory("interview-subagent")
        if not isinstance(m, (FilesystemPathGuardMiddleware, ErrorRecoveryMiddleware))
    ]
    middleware.append(
        FilesystemPathGuardMiddleware(workspace_root, allowed_write_paths=("/demand.md",))
    )

    graph = create_deep_agent(
        model=model,
        tools=[build_ask_user_tool()],
        system_prompt=system_prompt,
        middleware=middleware,
        backend=backend,
        # checkpointer=None：子代理在父代理 task 调用内执行，
        # 父代理 checkpointer 已捕获完整对话（含 interrupt 暂停状态）
        checkpointer=None,
    )
    return CompiledSubAgent(
        name="interview",
        description=(
            "适用：需要与用户多轮对话收集创作需求时调用。"
            "通过 ask_user 工具逐项提问，按 demand.md 模板填充核心/设定/风格/约束四层维度，"
            "维度齐全后请求用户确认成型。产出 demand.md，不挂评估。"
        ),
        runnable=graph,
    )
