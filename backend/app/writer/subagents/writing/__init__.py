# ==============================================================================
# Writing 子代理模块
#
# 导出写作管道的公共 API。
#
# 公共 API：
#   build_writing_subagent()          — 构建单独的 writing 子代理规格
#   build_writing_pipeline_subagent() — 构建带审查循环的 writing 管道子代理
# ==============================================================================

from app.writer.subagents.writing.writing_subagent import (
    build_writing_subagent,
    build_writing_pipeline_subagent,
)

__all__ = [
    "build_writing_subagent",
    "build_writing_pipeline_subagent",
]
