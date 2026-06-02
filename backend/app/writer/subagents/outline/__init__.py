# ==============================================================================
# Outline 子代理模块
#
# 导出 outline 管道的公共 API 和共享工具函数。
#
# 公共 API：
#   build_outline_subagent()          — 构建单独的 outline 子代理规格
#   build_outline_pipeline_subagent() — 构建带评估循环的 outline 管道子代理
#
# 共享工具函数（供 writing / detail_outline 模块复用）：
#   _agent_from_subagent_spec()       — 从规格构建可运行的代理
#   _build_compiled_pipeline_subagent() — 构建通用管道 StateGraph
#   其他工具函数（消息提取、上下文加载、产物校验等）
#
# 类型导出：
#   MiddlewareFactory, SecondaryDecision, SecondaryResultParser,
#   RevisionInstructionBuilder, ContextLoader
# ==============================================================================

from app.writer.subagents.outline.outline_subagent import (
    MiddlewareFactory,
    SecondaryDecision,
    SecondaryResultParser,
    RevisionInstructionBuilder,
    ContextLoader,
    build_outline_subagent,
    build_outline_pipeline_subagent,
    _agent_from_subagent_spec,
    _artifact_context,
    _build_compiled_pipeline_subagent,
    _evaluation_decision_to_secondary,
    _markdown_dir_context,
    _markdown_file_context,
    _messages_text,
    _required_result,
    _require_non_empty_artifact,
    _child_config,
    _accumulated_messages,
    _extract_text,
    _messages,
)

__all__ = [
    "MiddlewareFactory",
    "SecondaryDecision",
    "SecondaryResultParser",
    "RevisionInstructionBuilder",
    "ContextLoader",
    "build_outline_subagent",
    "build_outline_pipeline_subagent",
    # 共享 pipeline 工具函数（供 writing/detail_outline 导入）
    "_agent_from_subagent_spec",
    "_artifact_context",
    "_build_compiled_pipeline_subagent",
    "_evaluation_decision_to_secondary",
    "_markdown_dir_context",
    "_markdown_file_context",
    "_messages_text",
    "_required_result",
    "_require_non_empty_artifact",
    "_child_config",
    "_accumulated_messages",
    "_extract_text",
    "_messages",
]
