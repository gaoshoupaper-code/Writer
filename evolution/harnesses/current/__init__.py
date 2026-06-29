"""Writer 创作 Agent 包（Phase 7 harness 包化重构，D1=B 路径 B）。

包 = 自包含的 Agent 定义单元。执行端通过 assemble(ctx) 一行调用装配完整 agent。
运行时值（model/backend/checkpointer/workspace/trace）由 ctx 注入，不进包。

当前迁移状态（Phase 1 薄包装阶段）：
  - prompts/skills：已物理迁移进包（assemble 读包内文件）
  - middleware/subagents：薄包装 re-export 执行端现有实现（架构验证后物理迁移）
  - assemble：编排入口，复刻 _assemble_via_manifest 逻辑，数据源从 manifest 换成包目录

assemble 职责：
  - 读 prompts/*.md → system_prompt
  - 调执行端 build_deep_subagent / build_interview_deep_subagent 构建 subagent
  - 实例化 meta 层 middleware（含 TraceMiddleware，类由 ctx 注入，T2 设计）
  - 调 create_deep_agent 返回 CompiledAgent

设计依据：设计文档 D1=B / D5=② / D9 / T2。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from contracts.runtime_context import RuntimeContext

logger = logging.getLogger("harness_package")

# 包目录（本 __init__.py 所在目录）
PACKAGE_DIR = Path(__file__).resolve().parent


def _read_prompt(name: str) -> str:
    """读包内 prompts/<name>.md 文本。"""
    return (PACKAGE_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


def _skill_abs_paths(scope: str) -> list[str]:
    """取某 scope 的 skill 绝对路径列表（供 create_deep_agent 的 skills 参数）。

    scope='meta' → skills/meta/*
    scope='storybuilding' → skills/storybuilding-initial, storybuilding-expand
    scope='detail-outline' → skills/detail_outline/*
    scope='writing' → skills/writing/*
    """
    base = PACKAGE_DIR / "skills"
    if scope == "meta":
        return [str(base / "meta" / "auto-pipeline"), str(base / "meta" / "interactive-gating")]
    if scope == "storybuilding":
        return [str(base / "storybuilding-initial"), str(base / "storybuilding-expand")]
    if scope == "detail-outline":
        return [str(base / "detail_outline" / "detail-planning")]
    if scope == "writing":
        return [str(base / "writing" / "chapter-writing")]
    return []


def assemble(ctx: RuntimeContext, config: dict | None = None, source_root: Path | None = None):
    """装配完整创作 Agent（meta + 4 个 subagent）。

    入参 ctx 含全部运行时值（model/backend/checkpointer/workspace/trace/owner +
    trace_recorder + trace_middleware_cls）。包内只读 ctx，不依赖执行端其他状态。

    Phase 8 compose 配置化（决策 D4a/D9a/D12a）：
      - config + source_root 有值时：middleware 列表 + prompt 从 config 读（配置驱动）。
      - config 为 None 时：走原硬编码逻辑（向后兼容，渐进迁移）。
      - subagent 的专属逻辑（context 注入/style/storyline 约束）保留在 build_*（D12a），
        不从 config 读——config 只管通用 middleware 列表 + prompt 文本 + skills。

    TraceMiddleware 挂载（T2）：ctx.trace_middleware_cls 有值时实例化并插入 middleware
    列表（index 1，紧跟 ErrorRecovery）。类定义在执行端（不进包，D2'）。

    Args:
        ctx:         RuntimeContext（运行时值）
        config:      HarnessConfig JSON（可选，有值时配置驱动组装）
        source_root: 包根目录（可选，config 有值时用于 class_ref 解析 + prompt 读取）

    Returns: create_deep_agent 返回的 CompiledStateGraph。
    """
    # middleware + subagent + evaluator 已物理进包（包内相对 import）
    from .middleware.error_recovery import ErrorRecoveryMiddleware
    from .middleware.path_guard import FilesystemPathGuardMiddleware
    from .middleware.file_write_serialize import FileWriteSerializeMiddleware
    from .middleware.artifact_prerequisite import ArtifactPrerequisiteMiddleware, ArtifactPrerequisite
    from .middleware.meta_readonly import MetaReadOnlyMiddleware
    from .middleware.goal import GoalMiddleware
    from .subagents.interview import build_interview_deep_subagent
    from .subagents.types import apply_style_suffix
    # runtime 是 DeepAgents SDK 隔离层（执行端平台能力，包依赖它如同依赖 deepagents）
    from app.platform.agent.runtime import (
        GENERAL_PURPOSE_SUBAGENT, SubAgent, compose_skills_backend, create_deep_agent,
    )

    workspace_path = ctx.workspace_path
    styles = ctx.styles or {}

    # ── 硬编码 middleware 工厂（config 为 None 时用，或作为 fallback）──
    def _hardcoded_meta_middleware() -> list:
        return [
            ErrorRecoveryMiddleware(),
            MetaReadOnlyMiddleware(),
            FilesystemPathGuardMiddleware(workspace_path),
            FileWriteSerializeMiddleware(),
            GoalMiddleware(),
        ]

    def _hardcoded_subagent_middleware() -> list:
        return [
            ErrorRecoveryMiddleware(),
            FilesystemPathGuardMiddleware(workspace_path),
            FileWriteSerializeMiddleware(),
        ]

    # ── 配置驱动：从 config 读 middleware 列表（D4a/D9a）──
    use_config = config is not None and source_root is not None
    if use_config:
        # 延迟 import：assembler 在执行端，包不直接依赖它（通过 source_root 加载）
        import importlib
        # assembler 在 executor 侧，包内通过 app.platform.agent.assembler 调用
        from app.platform.agent.assembler import build_middleware_list
        # source_root 对应的包模块（已由 loader 加载到 sys.modules）
        pkg_module = importlib.import_module("harness_current")

        meta_pipeline = config.get("meta_pipeline", {})
        meta_middleware = build_middleware_list(
            meta_pipeline.get("processors", []), ctx, pkg_module,
        )
        # config 驱动的 prompt：从 config 读 prompt body（内容型 slot）
        meta_prompt_slot = meta_pipeline.get("slots", {}).get("system_prompt", {})
        meta_prompt_body = meta_prompt_slot.get("params", {}).get("body", "")
        meta_prompt = apply_style_suffix(meta_prompt_body, styles.get("meta"))
        # skills 从 config 读
        meta_skills_rel = meta_pipeline.get("slots", {}).get("skills", [])
        meta_skills = [str(source_root / s) for s in meta_skills_rel]
    else:
        # 硬编码路径（向后兼容）
        meta_middleware = _hardcoded_meta_middleware()
        meta_prompt = apply_style_suffix(_read_prompt("meta_system"), styles.get("meta"))
        meta_skills = _skill_abs_paths("meta")

    # TraceMiddleware 挂载（T2：类由 ctx 注入，包内实例化）
    if ctx.trace_recorder is not None and ctx.trace_id and ctx.trace_middleware_cls:
        meta_middleware.insert(1, ctx.trace_middleware_cls(
            ctx.trace_recorder, ctx.trace_id, "meta-agent",
        ))

    # interview 直通：workspace 已有 confirmed demand.md 时挂 DemandPreloadMiddleware
    # （评估集自动跑场景。生产交互路径 demand.md 由 interview 产出，不会预先 confirmed）
    demand_path = workspace_path / "demand.md"
    if demand_path.exists():
        try:
            if "status: confirmed" in demand_path.read_text(encoding="utf-8")[:300]:
                from .middleware.demand_preload import DemandPreloadMiddleware
                meta_middleware.append(DemandPreloadMiddleware(workspace_path))
        except Exception:
            pass

    # ── meta skills backend 组合 ──
    effective_backend, skill_sources = compose_skills_backend(ctx.backend, meta_skills)

    # ── subagent middleware 工厂 ──
    def middleware_factory(agent_name: str) -> list:
        if use_config:
            # 从 config 读对应 subagent 的 processors
            # agent_name 映射到 config 的 subagent key（去 -subagent 后缀 + 连字符转下划线）
            key = agent_name.replace("-subagent", "").replace("-", "_")
            sub_cfg = config.get("subagents", {}).get(key, {})
            mw = build_middleware_list(
                sub_cfg.get("processors", []), ctx, pkg_module,
            )
        else:
            mw = _hardcoded_subagent_middleware()
        if ctx.trace_recorder is not None and ctx.trace_id and ctx.trace_middleware_cls:
            mw.insert(1, ctx.trace_middleware_cls(
                ctx.trace_recorder, ctx.trace_id, agent_name,
            ))
        return mw

    # ── subagent 装配 ──
    subagents: list = []

    # 1. general-purpose（DeepAgents 自带规格）
    gp_spec = SubAgent(**GENERAL_PURPOSE_SUBAGENT)
    gp_spec["middleware"] = middleware_factory("general-purpose-subagent")
    subagents.append(gp_spec)

    # 2. interview（custom，无 review）
    subagents.append(build_interview_deep_subagent(
        workspace_path, ctx.model, ctx.backend, middleware_factory,
    ))

    # 3-5. deep subagents（各自完整装配：prompt/skills/专属middleware/reviewer）
    # 调用包内 build_*_deep_subagent，它们含全部专属逻辑（storyline 单线约束 /
    # context 注入 / skills 路径 / style suffix），assemble 不重复这些。
    from .subagents.storybuilding import build_storybuilding_deep_subagent
    from .subagents.detail_outline import build_detail_outline_deep_subagent
    from .subagents.writing import build_writing_deep_subagent

    subagents.append(build_storybuilding_deep_subagent(
        workspace_path, ctx.model, ctx.backend, middleware_factory,
        style_suffix=styles.get("storybuilding"),
    ))
    subagents.append(build_detail_outline_deep_subagent(
        workspace_path, ctx.model, ctx.backend, middleware_factory,
        style_suffix=styles.get("detail-outline"),
        context_file_paths=["outline.md", "character/*.md", "storyline.md", "storyline/*.md"],
    ))
    subagents.append(build_writing_deep_subagent(
        workspace_path, ctx.model, ctx.backend, middleware_factory,
        style_suffix=styles.get("writing"),
        context_file_paths=["outline.md", "storyline.md", "storyline/*.md", "character/*.md"],
    ))

    # ── 装配 meta agent ──
    return create_deep_agent(
        model=ctx.model,
        tools=[],
        system_prompt=meta_prompt,
        subagents=subagents,
        backend=effective_backend,
        checkpointer=ctx.checkpointer,
        middleware=meta_middleware,
        skills=skill_sources,
    )


__all__ = ["assemble"]
