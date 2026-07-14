"""Detail Outline 子代理 — 逐章细纲生成 + review 审查循环。"""

from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from app.platform.agent.middleware import ContextAssemblerMiddleware
from app.platform.agent.runtime import (
    BackendProtocol,
    CompiledSubAgent,
    FilesystemPermission,
    MiddlewareFactory,
    SubAgent,
    SubAgentSpec,
)
from .factory import build_deep_subagent
from .reviewers.detail_outline import build_detail_outline_reviewer
from .types import apply_style_suffix

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
    system_prompt = apply_style_suffix(
        PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix
    )
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
    """构建基于 DeepAgent 的 detail-outline 子代理（内含 review 审查循环）。

    替代 build_detail_outline_pipeline_subagent 的 StateGraph 管道模式。
    子代理自主决策：生成 → 调用 review 审查 → 根据反馈修订（单次审查修订）。

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

    # ---- review 子代理规格 ----
    # 注意：context_file_paths 只注入「审查基准类」文件（大纲/人物/剧情线），
    # 刻意排除审查对象本体 detail/*.md。原因：detail/*.md 全量注入会超过
    # FilesystemMiddleware 的消息落盘阈值，被替换为「内容过大已存盘」占位符，
    # 导致审查子代理实际看不到待审查内容而空转秒退。
    # 待审查的本批 chapter-XX.md 由父代理在 task description 中给出明确路径，
    # 审查子代理自行 read_file 读取（见 detail_outline_review.md 步骤 1）。
    review_spec = build_detail_outline_reviewer(
        workspace_root,
        middleware_factory("detail-outline-review-subagent"),
        context_file_paths=["outline.md", "character/*.md", "storyline.md", "storyline/*.md"],
    )

    # ---- 构建 review SubAgent dict ----
    review = SubAgent(
        name="review",
        description="审查 detail/ 下细纲文件的质量，写入 review/detail.md，返回评分和修订建议。",
        system_prompt=review_spec["system_prompt"],
        permissions=review_spec.get("permissions"),
        middleware=review_spec.get("middleware"),
    )

    # ---- 组装 system prompt ----
    system_prompt = primary_spec["system_prompt"]

    # ---- Skill 路径 ----
    skills_path = str(Path(__file__).resolve().parent.parent / "skills" / "detail_outline")

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="detail-outline",
        description=(
            "适用：storybuilding 产出 timeline.md 后，需要把事件编排进章节时调用。"
            "每次处理 timeline 的下一批 5-8 个事件，自主决定分几章、每章几事件，"
            "写入 detail/chapter-XX.md 并增量更新 detail/overview.md。"
            "内置 review 审查：批次生成后统一审查质量，如建议修订则自动修订（单次，仅 1 次）。"
            "委托时请说明创作目标（可选）。"
        ),
        model=model,
        system_prompt=system_prompt,
        review_spec=review,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        max_revisions=1,
        skills=[skills_path],
    )
