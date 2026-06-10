"""Storybuilding 子代理 — 两层渐进式故事构建。

架构：
- 总纲层：管理全局时间轴和锚点（叙事里程碑），写入 outline.md
- 卷纲层：管理相邻锚点之间的故事线、事件、钩子，写入 volume/XX.md

同时管理人物（character/*.md）和世界观（worldview.md），
由单代理统一维护跨维度一致性。

故事线信息内嵌于 volume/ 文件和 outline.md 故事线全局表，
不再使用独立的 storyline/ 目录。

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
from app.writer.expert_agent.types import apply_style_suffix
from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "storybuilding_system.md"
SKILLS_PATH_BASE = Path(__file__).resolve().parent.parent / "skills"


def build_storybuilding_subagent(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    style_suffix: str | None = None,
) -> SubAgent:
    """构建故事构建子代理规格。

    写入权限覆盖 4 个维度：character/, worldview.md, outline.md, volume/
    故事线信息内嵌于 volume/ 文件 + outline.md 全局表。

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
        # 写入：允许写入 4 个维度
        FilesystemPermission(operations=["write"], paths=["/character/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/worldview.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/outline.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/volume/*.md"], mode="allow"),
        # 拒绝：禁止写入其他所有文件
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]

    spec = SubAgent(
        name="storybuilding",
        description=(
            "适用：需要构建或扩展小说故事世界时调用——包括人物、世界观、"
            "总纲（锚点时间轴）和卷纲（故事线+事件+钩子）。"
            "两层渐进式架构：总纲管锚点，卷纲管线和事件。"
            "支持增量迭代：初构建首卷骨架，增量在已有骨架上扩展。"
            "内置统一评估：每轮产出后自动评估跨维度一致性，最多 3 轮修订。"
            "委托时必须说明：使用初构还是增量 Skill、本轮焦点、用户扩展方向。"
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
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 storybuilding 子代理（含统一评估循环）。

    子代理自主决策：产出/扩展各维度 → 调用 evolution 统一评估 → 根据反馈修订。
    默认不使用 ContextAssemblerMiddleware——agent 根据任务自主读取所需文件。
    但可通过 context_file_paths 注入指定文件（如 demand.md）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:  中间件工厂函数，按 agent_name 生成对应中间件列表
        style_suffix:        风格 SUFFIX 文本（可选）
        context_file_paths:  需要通过中间件自动注入的文件路径列表（可选）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 middleware ----
    storybuilding_middleware = list(middleware_factory("storybuilding-subagent"))
    if context_file_paths:
        storybuilding_middleware.append(ContextAssemblerMiddleware(
            workspace_root,
            file_paths=context_file_paths,
            context_label="创作需求",
        ))
    primary_spec = build_storybuilding_subagent(
        workspace_root, storybuilding_middleware, style_suffix
    )

    # ---- 统一评估子代理规格（评估器自主读取所有文件） ----
    evaluation_spec = build_storybuilding_evaluator(
        workspace_root,
        middleware_factory("storybuilding-evaluation-subagent"),
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description=(
            "统一评估所有故事维度（人物、世界观、总纲、卷纲）的跨维度一致性。"
            "自主读取所有产物，写入 evaluation.md，返回评分和修订建议。"
        ),
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- Skill 路径：初构 + 增量 ----
    skills = [
        str(SKILLS_PATH_BASE / "storybuilding-initial"),
        str(SKILLS_PATH_BASE / "storybuilding-expand"),
    ]

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="storybuilding",
        description=(
            "适用：需要构建或扩展小说故事世界时调用——包括人物、世界观、"
            "总纲（锚点时间轴）和卷纲（故事线+事件+钩子）。"
            "两层渐进式架构：总纲管锚点，卷纲管线和事件。"
            "支持增量迭代：初构建首卷骨架，增量在已有骨架上扩展。"
            "内置统一评估：每轮产出后自动评估跨维度一致性，最多 3 轮修订。"
            "委托时必须说明：使用初构还是增量 Skill、本轮焦点、用户扩展方向。"
        ),
        model=model,
        system_prompt=primary_spec["system_prompt"],
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        artifact_paths=[workspace_root / "outline.md"],
        max_revisions=3,
        skills=skills,
    )
