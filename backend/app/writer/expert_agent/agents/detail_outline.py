"""Detail Outline 子代理 — 逐章细纲生成 + evolution 评估循环。"""

from __future__ import annotations

from pathlib import Path

from deepagents import CompiledSubAgent, SubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.expert_agent.factory import build_deep_subagent
from app.writer.expert_agent.evaluators.detail_outline import build_detail_outline_evaluator
from app.writer.expert_agent.types import MiddlewareFactory, SubAgentSpec, apply_style_suffix

# 细纲子代理的系统提示词文件路径
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "detail_outline_system.md"


def build_detail_outline_subagent(
    middleware: list[AgentMiddleware] | None = None,
    style_suffix: str | None = None,
) -> SubAgentSpec:
    """构建细纲子代理规格。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /detail/**（细纲文件）
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:     额外中间件列表（可选）
        style_suffix:  细纲风格 SUFFIX 文本（可选）

    Returns:
        子代理规格字典
    """
    system_prompt = apply_style_suffix(PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix)
    permissions = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/detail/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
    spec = SubAgentSpec(
        name="detail-outline",
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_detail_outline_deep_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 detail-outline 子代理（内含 evolution 评估循环）。

    替代 build_detail_outline_pipeline_subagent 的 StateGraph 管道模式。
    子代理自主决策：生成 → 调用 evolution 评估 → 根据反馈修订（最多 3 轮）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        细纲风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 system prompt + middleware ----
    project_middleware = list(middleware_factory("detail-outline-subagent"))
    project_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
    ))
    primary_spec = build_detail_outline_subagent(project_middleware, style_suffix)

    # ---- evolution 子代理规格 ----
    evaluation_spec = build_detail_outline_evaluator(
        workspace_root,
        middleware_factory("detail-outline-evaluation-subagent"),
        context_file_paths=["outline.md", "character/*.md", "storyline/*.md", "volume/*.md", "detail/*.md"],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description="评估 detail/ 下细纲文件的质量，写入 detail/evaluation.md，返回评分和修订建议。",
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- 组装 system prompt ----
    system_prompt = primary_spec["system_prompt"]

    # ---- Skill 路径 ----
    skills_path = str(Path(__file__).resolve().parent.parent / "skills")

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="detail-outline",
        description=(
            "适用：卷纲通过 evaluation 后，需要将卷纲拆解为逐章细纲时调用。"
            "每次调用批量生成 3 章细纲文件（chapter-XX.md ~ chapter-XX+2.md），"
            "内置 evolution 评估循环：3 章生成后统一评估质量，如果评估建议修订会自动修订，最多 3 轮。"
            "主代理控制批次推进节奏：每次指定起始章节编号。"
            "委托时请说明本次要生成的起始章节编号和创作目标。"
        ),
        model=model,
        system_prompt=system_prompt,
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        max_revisions=3,
        skills=[skills_path],
    )
