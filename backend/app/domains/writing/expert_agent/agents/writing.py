"""Writing 子代理 — 正文写作 + evolution 审查循环。"""

from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware.types import AgentMiddleware

from app.platform.agent.runtime import (
    BackendProtocol,
    CompiledSubAgent,
    FilesystemPermission,
    MiddlewareFactory,
    SubAgent,
    SubAgentSpec,
)
from langchain_core.language_models import BaseChatModel

from app.platform.agent.middleware import ContextAssemblerMiddleware
from app.domains.writing.expert_agent.factory import build_deep_subagent
from app.domains.writing.expert_agent.evaluators.writing import build_writing_evaluator
from app.domains.writing.expert_agent.types import apply_style_suffix

# 写作子代理的系统提示词文件路径
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "writing_system.md"


def build_writing_subagent(middleware: list[AgentMiddleware] | None = None, style_suffix: str | None = None) -> SubAgentSpec:
    """构建单独的 writing 子代理规格（不含审查管道）。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /chapter/** （正文章节）
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:     额外中间件列表（可选）
        style_suffix:  写作风格 SUFFIX 文本（可选，追加到系统提示词末尾）

    Returns:
        子代理规格字典
    """
    system_prompt = apply_style_suffix(PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix)
    permissions = [
        FilesystemPermission(
            operations=["read"],
            paths=["/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/chapter/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

    spec = SubAgentSpec(
        name="writing",
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_writing_deep_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 writing 子代理（内含 evolution 审查循环）。

    子代理自主决策：写作 → 调用 evolution 审查 → 根据反馈修订（单次审查修订）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        写作风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 system prompt + middleware ----
    writing_middleware = list(middleware_factory("writing-subagent"))
    writing_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
        context_label="写作前置上下文",
    ))
    primary_spec = build_writing_subagent(writing_middleware, style_suffix)

    # ---- evolution 子代理规格 ----
    # 注意：context_file_paths 只注入「评估基准类」文件（大纲/剧情线/人物），
    # 刻意排除评估对象本体 chapter/*.md 和细纲 detail/*.md。原因：
    # chapter/*.md 全量注入会超过 FilesystemMiddleware 的消息落盘阈值，被替换为
    # 「内容过大已存盘」占位符，导致审查子代理实际看不到待审查正文而空转秒退。
    # 待审查的本章文件路径由父代理在 task description 中给出，审查子代理自行
    # read_file 读取（见 writing_evaluation.md 步骤 1）。detail/ 细纲也由子代理
    # 按需 read_file 做一致性核对，不作为前置上下文注入。
    evaluation_spec = build_writing_evaluator(
        workspace_root,
        middleware_factory("writing-evaluation-subagent"),
        context_file_paths=["outline.md", "storyline.md", "storyline/*.md", "character/*.md"],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description="审查正文章节质量，写入 review/ 下对应文件，返回审查结论和修订建议。",
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- 组装 system prompt ----
    system_prompt = primary_spec["system_prompt"]

    # ---- Skill 路径 ----
    skills_path = str(Path(__file__).resolve().parent.parent / "skills" / "writing")

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="writing",
        description=(
            "适用：需要生成、追加或修订单个正文章节时调用；不用于大纲、角色或评估。"
            "内置 evolution 审查：写作后自动审查质量，如果审查建议修订会自动修订（单次审查修订，仅 1 次）。"
            "输入上下文包含 character/（角色设计）、outline.md（大纲剧情）和 detail/（对应细纲）。"
            "委托时请说明章节编号、本章目标、出场人物和关键约束。"
        ),
        model=model,
        system_prompt=system_prompt,
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        max_revisions=1,
        skills=[skills_path],
    )
