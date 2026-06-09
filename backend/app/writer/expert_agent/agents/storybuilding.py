"""Storybuilding 子代理 — 增量式故事构建（人物+故事线+世界观+总纲+卷纲）。

替代原 character.py + outline.py，用单一 agent 统一管理五个创作维度。
每轮产出后自动调用 evolution 进行跨维度统一评估，最多 3 轮修订。

导出的公共 API：
  - build_storybuilding_subagent():        构建故事构建子代理规格
  - build_storybuilding_deep_subagent():   构建 DeepAgent 版本（含统一评估循环）
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deepagents import CompiledSubAgent, SubAgent
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.expert_agent.factory import build_deep_subagent
from app.writer.expert_agent.evaluators.storybuilding import build_storybuilding_evaluator
from app.writer.expert_agent.types import SubAgentSpec, apply_style_suffix
from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "storybuilding_system.md"


def build_storybuilding_subagent(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    style_suffix: str | None = None,
) -> SubAgent:
    """构建故事构建子代理规格。

    写入权限覆盖 5 个维度：character/, storyline/, worldview.md, outline.md, volume/

    Args:
        workspace_root: 工作区根目录
        middleware:     额外中间件列表（可选）
        style_suffix:   风格 SUFFIX 文本（可选）

    Returns:
        故事构建子代理规格字典
    """
    system_prompt = apply_style_suffix(
        PROMPT_PATH.read_text(encoding="utf-8").strip(),
        style_suffix,
    )

    permissions = [
        # 读取：允许读取所有文件
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        # 写入：允许写入 5 个维度
        FilesystemPermission(operations=["write"], paths=["/character/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/storyline/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/worldview.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/outline.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/volume/*.md"], mode="allow"),
        # 拒绝：禁止写入其他所有文件
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]

    spec = SubAgent(
        name="storybuilding",
        description=(
            "适用：需要构建或扩展小说故事世界时调用——包括人物、故事线、世界观、总纲、卷纲。"
            "支持增量迭代：每轮基于已有产物扩展，同时维护跨维度一致性。"
            "内置统一评估：每轮产出后自动评估所有维度的跨维度一致性和完整性。"
            "委托时必须说明：当前轮次、用户扩展方向（如有）、本轮焦点维度。"
        ),
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_storybuilding_deep_subagent(
    workspace_root: Path,
    model: object,
    backend: object,
    middleware_factory: Callable[[str], list[AgentMiddleware]],
    style_suffix: str | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 storybuilding 子代理（含统一评估循环）。

    替代原 build_character_deep_subagent + build_outline_deep_subagent。
    子代理自主决策：产出/扩展各维度 → 调用 evolution 统一评估 → 根据反馈修订（最多 3 轮）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:  中间件工厂函数，按 agent_name 生成对应中间件列表
        style_suffix:        风格 SUFFIX 文本（可选）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 middleware：注入已有产物作为上下文 ----
    storybuilding_middleware = list(middleware_factory("storybuilding-subagent"))
    storybuilding_middleware.append(
        ContextAssemblerMiddleware(
            workspace_root,
            file_paths=[
                "character/*.md",
                "storyline/*.md",
                "worldview.md",
                "outline.md",
                "volume/*.md",
            ],
            context_label="已有故事产物",
        )
    )
    primary_spec = build_storybuilding_subagent(
        workspace_root, storybuilding_middleware, style_suffix
    )

    # ---- 统一评估子代理规格 ----
    evaluation_spec = build_storybuilding_evaluator(
        workspace_root,
        middleware_factory("storybuilding-evaluation-subagent"),
        context_file_paths=[
            "character/*.md",
            "storyline/*.md",
            "worldview.md",
            "outline.md",
            "volume/*.md",
        ],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description=(
            "统一评估所有故事维度（人物、故事线、世界观、总纲、卷纲）的跨维度一致性。"
            "读取所有产物，写入 evaluation.md，返回评分和修订建议。"
        ),
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- Skill 路径 ----
    skills_path = str(Path(__file__).resolve().parent.parent / "skills")

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="storybuilding",
        description=(
            "适用：需要构建或扩展小说故事世界时调用——包括人物、故事线、世界观、总纲、卷纲。"
            "支持增量迭代：每轮基于已有产物扩展，同时维护跨维度一致性。"
            "内置统一评估：每轮产出后自动评估所有维度的跨维度一致性和完整性，最多 3 轮修订。"
            "委托时必须说明：当前轮次、用户扩展方向（如有）、本轮焦点维度。"
        ),
        model=model,
        system_prompt=primary_spec["system_prompt"],
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        artifact_paths=[workspace_root / "outline.md"],
        max_revisions=3,
        skills=[skills_path],
    )
