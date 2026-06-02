# ==============================================================================
# Detail Outline 子代理模块
#
# 导出细纲管道的公共 API。
#
# 公共 API：
#   build_detail_outline_subagent()          — 构建细纲子代理规格
#   build_detail_outline_pipeline_subagent() — 构建多阶段细纲管道子代理
# ==============================================================================

from app.writer.subagents.detail_outline.detail_outline_subagent import (
    build_detail_outline_subagent,
    build_detail_outline_pipeline_subagent,
)

__all__ = [
    "build_detail_outline_subagent",
    "build_detail_outline_pipeline_subagent",
]
