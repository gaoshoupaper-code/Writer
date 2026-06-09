"""DeepAgent 子代理工厂模块。

将各创作型子代理（outline / detail_outline / writing / character）构建为
create_deep_agent 实例，内部注册 evolution SubAgent 进行评估。

替代原 _build_compiled_pipeline_subagent 的 StateGraph 管道模式，
改为让 DeepAgent 自主决策"生成 → 调用 evolution 评估 → 根据反馈修订"的流程。
"""
from __future__ import annotations

from pathlib import Path

from deepagents import CompiledSubAgent, SubAgent, create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from app.writer.middleware.artifact_validation_middleware import ArtifactValidationMiddleware
from app.writer.middleware.revision_limit_middleware import RevisionLimitMiddleware


def build_deep_subagent(
    *,
    name: str,
    description: str,
    model: BaseChatModel,
    system_prompt: str,
    evolution_spec: SubAgent,
    subagent_middleware: list[AgentMiddleware] | None = None,
    backend: object | None = None,
    artifact_paths: list[Path] | None = None,
    max_revisions: int = 3,
) -> CompiledSubAgent:
    """将创作型子代理构建为 DeepAgent（内含 evolution 评估子代理）。

    架构：
      create_deep_agent(
          subagents=[evolution SubAgent],
          middleware=[...项目中间件, RevisionLimitMiddleware, ArtifactValidationMiddleware]
      ) → 包装为 CompiledSubAgent 返回

    流程（由 DeepAgent 自主决策）：
      1. 接收父代理委托，生成/修改创作产物
      2. 调用 evolution 子代理评估产物质量
      3. 根据 evolution 返回的评估结果决定是否修订
      4. 修订后再次调用 evolution（最多 max_revisions 轮）
      5. 向父代理返回汇总结果

    Args:
        name:                子代理名称（如 "outline"）
        description:         子代理功能描述（供父代理选择委托目标）
        model:               聊天模型
        system_prompt:       子代理系统提示词（含修订指令）
        evolution_spec:      已构建好的 evolution SubAgent 规格字典
                             （由调用方通过 build_evaluation_subagent 构建）
        subagent_middleware: 子代理的额外中间件（可选，由调用方注入 PathGuard/Trace/Goal 等）
        backend:             DeepAgents 后端（文件系统）
        artifact_paths:      期望的产物文件路径列表（用于 ArtifactValidationMiddleware）
        max_revisions:       最大修订（evolution 调用）次数，默认 3

    Returns:
        编译后的子代理字典 {name, description, runnable}，可直接注册到父代理
    """
    # ---- 1. 组装子代理 middleware ----
    mw: list[AgentMiddleware] = list(subagent_middleware) if subagent_middleware else []
    # RevisionLimitMiddleware 拦截 evolution 调用次数，提供硬上限
    mw.append(RevisionLimitMiddleware(max_revisions=max_revisions))
    # ArtifactValidationMiddleware 在代理输出前检查产物文件（可选）
    if artifact_paths:
        mw.append(ArtifactValidationMiddleware(artifact_paths))

    # ---- 2. 调用 create_deep_agent ----
    graph = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=system_prompt,
        subagents=[evolution_spec],
        middleware=mw,
        backend=backend,
        # checkpointer=None: 子代理在父代理的 task 工具调用内执行，
        # 父代理的 checkpointer 已捕获完整对话历史，无需独立持久化
        checkpointer=None,
    )

    # ---- 3. 包装为 CompiledSubAgent ----
    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=graph,
    )
