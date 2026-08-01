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


def _build_memory_recall_middleware(ctx: RuntimeContext):
    """根据 ctx 条件构建 MemoryRecallMiddleware（记忆系统未启用时返回 None）。

    复用 T2 注入模式：ctx.memory_backend（实例）+ ctx.memory_recall_middleware_cls（类）
    都非 None 时，在包内实例化 middleware。workspace_id 从 owner_id + workspace 名算出。
    None 时返回 None → writing 子代理走 ContextAssembler 全量注入（向后兼容）。

    NWM 重构（Phase 5）：harness 可进化要素注入——
      - query_builder：harness tools/query_builder.py（task→writer query）
      - join_rules + packet_formatter：注入 MemoryRetriever（覆盖 executor 默认）
    注入用 executor 的 set_memory_retriever 进程单例。

    FR-003（DEC-003）：每次 assemble 都注入**本包**的检索要素，不再用进程级
    "只注入一次"保护。同进程热加载多版本（v6→v7）时，每个版本装配自身 join_rules/
    packet_formatter，杜绝首版本锁死后续版本（CON-002 版本隔离）。要素加载用本包的
    PACKAGE_DIR（而非 load_current_package 的生产缓存包），保证 A/B 候选包注入的是
    候选版本的要素。

    P4 进化闭环：构造 quality_callback 闭包，把检索质量写到 trace run_meta 事件。
    trace_recorder 或 trace_id 为 None 时不埋点（向后兼容）。
    """
    if ctx.memory_backend is None or ctx.memory_recall_middleware_cls is None:
        return None

    # workspace_id（与 events.py 一致：owner_id_workspace_name，定位 memory.db）
    ws_name = ctx.workspace_path.name
    workspace_id = f"{ctx.owner_id}_{ws_name}" if ctx.owner_id else ws_name
    # group_id 兼容旧签名（backend 内部用 workspace_id，group_id 仅日志/兼容）
    group_id = workspace_id

    # ── Phase 5 / FR-003：每次注入本包 harness 可进化检索要素（去单例保护）──
    _inject_harness_retriever()

    # P4：构造检索质量埋点回调（写 trace run_meta 事件）
    quality_callback = _make_quality_callback(ctx)

    # query_builder 从 harness 加载（middleware _build_query 可被覆盖）
    query_builder = _load_harness_query_builder()

    return ctx.memory_recall_middleware_cls(
        backend=ctx.memory_backend,
        group_id=group_id,
        workspace_path=ctx.workspace_path,
        quality_callback=quality_callback,
        query_builder=query_builder,  # None 时 middleware 用内置 _build_query
    )


# FR-003：注入锁——保证并发 assemble 时 set_memory_retriever 不撕裂。
# A/B 后台任务并发跑不同 harness 版本时（EDGE-004），若两个 assemble 同时
# set_memory_retriever，可能造成短暂的全局 retriever 指向错版本。用锁串行化
# 注入动作，每次 assemble 都重新设当前版本（要素是无状态的，retrieve 时从参数
# 取 store，重设只影响下一次检索用哪个 join_rules/formatter）。
import threading as _threading
_harness_retriever_lock = _threading.Lock()


def _inject_harness_retriever() -> None:
    """把本包的 join_rules + packet_formatter 注入 executor MemoryRetriever。

    FR-003（DEC-003）：移除"只注入一次"进程级单例保护，每次 assemble 重新注入当前
    harness 包版本的检索要素。同进程热加载 v6→v7 时 v7 注入自身要素，不被 v6 锁死。
    要素加载用本包 PACKAGE_DIR（importlib 按 __name__ 定位），不依赖 load_current_package
    的生产缓存包——保证 A/B 候选包注入候选版本要素（CON-002 版本隔离）。

    要素加载失败时静默回退到 executor 默认 retriever（降级语义不变）。注入用锁串行化，
    防并发 assemble 时全局 retriever 撕裂（EDGE-004）。
    """
    try:
        from app.platform.memory.retriever import MemoryRetriever, set_memory_retriever
        join_rules = _load_harness_callable("join_rules", "join_rules")
        packet_formatter = _load_harness_callable("packet_formatter", "packet_formatter")
        if join_rules is None and packet_formatter is None:
            return  # 本包无 harness 要素覆盖，保留 executor 默认 retriever
        retriever = MemoryRetriever(
            join_rules=join_rules,
            packet_formatter=packet_formatter,
        )
        with _harness_retriever_lock:
            set_memory_retriever(retriever)
    except Exception as e:
        # harness 要素注入失败不阻断——executor 默认 retriever 仍可用
        logger.debug("harness retriever 注入失败，用 executor 默认：%s", e)


def _load_harness_query_builder():
    """加载 harness tools/query_builder.build_query（None 用 middleware 内置）。"""
    return _load_harness_callable("query_builder", "build_query")


def _load_harness_callable(module_name: str, func_name: str):
    """从**本包** tools 子包加载可调用对象（失败返回 None，降级到 executor 默认）。

    FR-003：用 importlib 按 __name__（当前包名）加载，而非 load_current_package 的
    生产缓存包。这样 A/B 候选包（harness_current_ab）加载自身 tools，生产包
    （harness_current）加载生产 tools，版本隔离正确。
    """
    try:
        import importlib
        mod = importlib.import_module(f"{__name__}.tools.{module_name}")
        return getattr(mod, func_name, None)
    except Exception:
        return None


def _make_quality_callback(ctx: RuntimeContext):
    """构造记忆检索质量埋点回调。

    middleware 每次检索后调用此回调，传入 TraceMemoryQuality 字段 dict。
    回调用 trace_recorder.append_event 写一条 run_meta 事件到 trace。

    trace_recorder 或 trace_id 为 None 时返回 None（不埋点）。
    """
    recorder = ctx.trace_recorder
    trace_id = ctx.trace_id
    if recorder is None or not trace_id:
        return None

    def _callback(quality_data: dict) -> None:
        try:
            recorder.append_event(trace_id, {
                "type": "run_meta",
                "source": "middleware",
                "agent_name": "writing",
                "input": {"memory_quality": quality_data},
            })
        except Exception:
            # 埋点失败不影响写作流程（静默吞掉）
            pass

    return _callback


def _make_artifact_snapshot_callback(ctx: RuntimeContext):
    """构造不可变 ArtifactRevision 回调。"""
    if ctx.artifact_snapshot_callback is not None:
        return ctx.artifact_snapshot_callback

    recorder = ctx.trace_recorder
    trace_id = ctx.trace_id
    if recorder is None or not trace_id:
        return None
    record_revision = getattr(recorder, "record_artifact_revision", None)
    if not callable(record_revision):
        return None

    def _callback(snapshot_data: dict) -> None:
        try:
            record_revision(
                trace_id,
                snapshot_data.get("agent_name", "unknown"),
                file_path=snapshot_data["file_path"],
                content=snapshot_data.get("content", ""),
                tool_name=snapshot_data.get("tool"),
                tool_call_id=snapshot_data.get("tool_call_id"),
                content_hash=snapshot_data.get("fingerprint"),
            )
        except Exception:
            pass

    return _callback


def _make_intervention_callback(ctx: RuntimeContext, agent_name: str):
    recorder = ctx.trace_recorder
    trace_id = ctx.trace_id
    if recorder is None or not trace_id:
        return None

    def _callback(
        *, action: str, hook: str, affected_fields: list[str], reason: str | None = None,
        before: Any | None = None, after: Any | None = None,
    ) -> None:
        try:
            recorder.record_intervention(
                trace_id,
                agent_name,
                action=action,
                hook=hook,
                affected_fields=affected_fields,
                reason=reason,
                before=before,
                after=after,
            )
        except Exception:
            pass

    return _callback


def _retry_runner_for(ctx: RuntimeContext):
    """从 ctx 构造模型重试运行器（FR-003/EVD-006 可观测）。

    writer_retry_runner_factory 是 RuntimeContext 声明字段（默认 None）；None 时返回 None，
    TraceMiddleware 走原"只看外边界"行为（向后兼容）。meta 与各子代理装配共用此构造。
    """
    factory = ctx.writer_retry_runner_factory
    return factory() if factory is not None else None


def assemble(ctx: RuntimeContext):
    """装配完整创作 Agent（meta + 4 个 subagent）。

    入参 ctx 含全部运行时值（model/backend/checkpointer/workspace/trace/owner +
    trace_recorder + trace_middleware_cls）。包内只读 ctx，不依赖执行端其他状态。

    TraceMiddleware 挂载（T2）：ctx.trace_middleware_cls 有值时实例化并插入 middleware
    列表（index 1，紧跟 ErrorRecovery）。类定义在执行端（不进包，D2'）。

    Args:
        ctx: RuntimeContext（运行时值）

    Returns: create_deep_agent 返回的 CompiledStateGraph。
    """
    # middleware + subagent + evaluator 已物理进包（包内相对 import）
    from .middleware.error_recovery import ErrorRecoveryMiddleware
    from .middleware.path_guard import FilesystemPathGuardMiddleware
    from .middleware.file_write_serialize import FileWriteSerializeMiddleware
    from .middleware.artifact_prerequisite import ArtifactPrerequisiteMiddleware, ArtifactPrerequisite
    from .middleware.meta_readonly import MetaReadOnlyMiddleware
    from .middleware.goal import GoalMiddleware
    # A2 加固中间件（原孤儿代码，重新装配 + 修复设计缺陷）
    from .middleware.read_cache import ReadCacheMiddleware
    from .middleware.encoding_guard import EncodingGuardMiddleware
    from .middleware.file_state_tracker import FileStateTrackerMiddleware
    from .middleware.write_result_inspector import WriteResultInspectorMiddleware
    from .middleware.artifact_snapshot import ArtifactSnapshotMiddleware
    from .subagents.interview import build_interview_deep_subagent
    from .subagents.types import apply_style_suffix
    # runtime 是 DeepAgents SDK 隔离层（执行端平台能力，包依赖它如同依赖 deepagents）
    from app.platform.agent.runtime import (
        GENERAL_PURPOSE_SUBAGENT, SubAgent, compose_skills_backend, create_deep_agent,
    )

    workspace_path = ctx.workspace_path
    styles = ctx.styles or {}

    # ── meta middleware（硬编码，从包内类实例化）──
    # A2 重构后装配顺序（外层→内层）：
    #   ErrorRecovery（捕异常/重试，最外层）
    #   → MetaReadOnly（meta 只读）
    #   → ReadCache（命中短路，最外层拦截 read_file）
    #   → FilesystemPathGuard（路径白名单 + 规范化）
    #   → EncodingGuard（写入后编码+完整性校验；在 PathGuard 之后拿规范化路径）
    #   → FileStateTracker（edit_file 前 old_string 预检）
    #   → FileWriteSerialize（按 file_path 串行化写）
    #   → WriteResultInspector（在串行化内、ErrorRecovery 内，转抛 WriteFailedError）
    #   → GoalMiddleware（最内层）
    meta_middleware = [
        ErrorRecoveryMiddleware(
            intervention_callback=_make_intervention_callback(ctx, "meta-agent"),
            tool_replay_policy=ctx.tool_replay_policy,  # CON-005 task 防重放
        ),
        MetaReadOnlyMiddleware(intervention_callback=_make_intervention_callback(ctx, "meta-agent")),
        ReadCacheMiddleware(intervention_callback=_make_intervention_callback(ctx, "meta-agent")),
        FilesystemPathGuardMiddleware(
            workspace_path,
            intervention_callback=_make_intervention_callback(ctx, "meta-agent"),
        ),
        EncodingGuardMiddleware(),
        FileStateTrackerMiddleware(),
        FileWriteSerializeMiddleware(intervention_callback=_make_intervention_callback(ctx, "meta-agent")),
        WriteResultInspectorMiddleware(),
        GoalMiddleware(),
    ]
    meta_prompt = apply_style_suffix(_read_prompt("meta_system"), styles.get("meta"))
    meta_skills = _skill_abs_paths("meta")

    # TraceMiddleware 挂载（T2：类由 ctx 注入，包内实例化）
    # FR-003/EVD-006：写作路径注入重试运行器时，TraceMiddleware 用它包裹模型调用，
    # 让传输层 attempt/退避在 Trace 可见。运行器由领域层提供（平台层不 import domains）。
    if ctx.trace_recorder is not None and ctx.trace_id and ctx.trace_middleware_cls:
        meta_middleware.insert(1, ctx.trace_middleware_cls(
            ctx.trace_recorder, ctx.trace_id, "meta-agent", retry_runner=_retry_runner_for(ctx),
        ))

    # CreditsMiddleware 挂载（AD2/AD6：积分制，类由 ctx 注入，包内实例化）
    # 仅在有 owner_id（用户创作）且有 credits_service 时挂载。
    # meta agent 挂载：负责预扣触发（tool_call 检测 storybuilding）+ model_call 计费。
    if ctx.credits_service is not None and ctx.credits_middleware_cls and ctx.owner_id:
        meta_middleware.insert(
            1,
            ctx.credits_middleware_cls(
                ctx.credits_service, ctx.trace_id, ctx.owner_id,
                ctx.workspace_path, "meta-agent",
            ),
        )

    # ── meta skills backend 组合 ──
    effective_backend, skill_sources = compose_skills_backend(ctx.backend, meta_skills)

    # Trace V2：在装配事实发生点冻结可用 Skill catalog 与最终 middleware 顺序。
    # 运行时实际激活仍由 TraceMiddleware 在成功解析注册 Skill 后单独记录。
    if ctx.trace_recorder is not None and ctx.trace_id:
        record_catalog = getattr(ctx.trace_recorder, "record_skill_catalog", None)
        record_stack = getattr(ctx.trace_recorder, "record_middleware_assembly", None)
        if callable(record_catalog):
            record_catalog(ctx.trace_id, "meta-agent", meta_skills, skill_sources)
        if callable(record_stack):
            record_stack(ctx.trace_id, "meta-agent", meta_middleware)

    # ── subagent middleware 工厂 ──
    # A2 重构后装配顺序（与 meta 一致，去掉 MetaReadOnly 和 Goal）：
    #   ErrorRecovery（最外层）
    #   → ReadCache（命中短路）
    #   → FilesystemPathGuard（路径白名单）
    #   → EncodingGuard（写入校验）
    #   → FileStateTracker（edit_file 预检）
    #   → FileWriteSerialize（写串行化）
    #   → WriteResultInspector（最内层，转抛 WriteFailedError）
    def middleware_factory(agent_name: str) -> list:
        intervention_callback = _make_intervention_callback(ctx, agent_name)
        mw = [
            ErrorRecoveryMiddleware(
                intervention_callback=intervention_callback,
                tool_replay_policy=ctx.tool_replay_policy,  # CON-005 task 防重放
            ),
            ReadCacheMiddleware(intervention_callback=intervention_callback),
            FilesystemPathGuardMiddleware(
                workspace_path,
                intervention_callback=intervention_callback,
            ),
            EncodingGuardMiddleware(),
            FileStateTrackerMiddleware(),
            FileWriteSerializeMiddleware(intervention_callback=intervention_callback),
            WriteResultInspectorMiddleware(),
        ]
        if ctx.trace_recorder is not None and ctx.trace_id and ctx.trace_middleware_cls:
            mw.insert(1, ctx.trace_middleware_cls(
                ctx.trace_recorder, ctx.trace_id, agent_name, retry_runner=_retry_runner_for(ctx),
            ))
        # CreditsMiddleware 挂载到创作类子代理（AD6），不挂 interview（访谈免费）。
        if (
            ctx.credits_service is not None
            and ctx.credits_middleware_cls
            and ctx.owner_id
            and agent_name != "interview-subagent"
        ):
            mw.insert(1, ctx.credits_middleware_cls(
                ctx.credits_service, ctx.trace_id, ctx.owner_id,
                ctx.workspace_path, agent_name,
            ))
        # ArtifactSnapshotMiddleware 挂载到所有子代理（第二期证据采集，2026-07）。
        # 装在 WriteResultInspector 之后（最内层），只有写盘成功的才快照。
        artifact_cb = _make_artifact_snapshot_callback(ctx)
        if artifact_cb is not None:
            mw.append(ArtifactSnapshotMiddleware(artifact_cb, workspace_path, agent_name))
        if ctx.trace_recorder is not None and ctx.trace_id:
            catalog_scopes = {
                "storybuilding-subagent": "storybuilding",
                "detail-outline-subagent": "detail-outline",
                "writing-subagent": "writing",
            }
            scope = catalog_scopes.get(agent_name)
            if scope:
                paths = _skill_abs_paths(scope)
                _, runtime_sources = compose_skills_backend(ctx.backend, paths)
                record_catalog = getattr(ctx.trace_recorder, "record_skill_catalog", None)
                if callable(record_catalog):
                    record_catalog(ctx.trace_id, agent_name, paths, runtime_sources)
            record_stack = getattr(ctx.trace_recorder, "record_middleware_assembly", None)
            if callable(record_stack) and agent_name not in catalog_scopes:
                record_stack(ctx.trace_id, agent_name, mw)
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
        memory_recall_middleware=_build_memory_recall_middleware(ctx),
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
