# ==============================================================================
# writer.middleware 过渡门面（PR-04）
#
# 通用中间件实体已迁入 app.platform.agent.middleware（8 个）。
# 本文件保留 re-export 供 writer 内部 + 旧代码过渡期引用，避免大批 import 改动集中在此 PR。
# 阶段 B 闭合（PR-06）或 PR-11 writer 降级时删除。
#
# 仍在 writer 下的写作专属中间件（GoalMiddleware / MetaReadOnlyMiddleware）保留实体导出。
#
# transitional re-export —— PR-11 writer 降级时清理
# ==============================================================================

# 写作专属（实体留在此处，PR-11 随 writer 迁入 domains/writing/middleware）
from app.writer.middleware.goal_middleware import GoalMiddleware
from app.writer.middleware.meta_readonly_middleware import MetaReadOnlyMiddleware

# 通用件（实体已迁 platform，re-export 保持旧 import 路径可用）
from app.platform.agent.middleware import (
    ArtifactPrerequisite,
    ArtifactPrerequisiteMiddleware,
    ArtifactValidationMiddleware,
    ContextAssemblerMiddleware,
    ErrorRecoveryMiddleware,
    FilesystemPathGuardMiddleware,
    TraceCallbackHandler,
    TraceMiddleware,
)

__all__ = [
    "ArtifactPrerequisite",
    "ArtifactPrerequisiteMiddleware",
    "ArtifactValidationMiddleware",
    "ContextAssemblerMiddleware",
    "ErrorRecoveryMiddleware",
    "FilesystemPathGuardMiddleware",
    "GoalMiddleware",
    "MetaReadOnlyMiddleware",
    "TraceCallbackHandler",
    "TraceMiddleware",
]
