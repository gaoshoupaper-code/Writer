# ==============================================================================
# platform.agent.middleware —— 领域无关中间件（PR-04 实体迁入）
#
# 8 个通用中间件已从 writer/middleware（及 expert_agent/middleware）物理迁入此目录。
# 写作专属中间件（GoalMiddleware / MetaReadOnlyMiddleware）仍在 writer/middleware，
# PR-11 writer 降级时随迁到 domains/writing。
# trace 依赖已随 PR-05 切到 app.platform.trace。
# ==============================================================================

from app.platform.agent.middleware.artifact_prerequisite_middleware import (
    ArtifactPrerequisite,
    ArtifactPrerequisiteMiddleware,
)
from app.platform.agent.middleware.artifact_validation_middleware import ArtifactValidationMiddleware
from app.platform.agent.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.platform.agent.middleware.error_recovery_middleware import ErrorRecoveryMiddleware
from app.platform.agent.middleware.file_write_serialize import FileWriteSerializeMiddleware
from app.platform.agent.middleware.path_guard_middleware import (
    WRITING_WRITE_PATTERNS,
    FilesystemPathGuardMiddleware,
)
from app.platform.agent.middleware.trace_callback import TraceCallbackHandler
from app.platform.agent.middleware.trace_middleware import TraceMiddleware

# WORKSPACE_WRITE_PATTERNS：写作默认写路径白名单的通用别名（领域无关视角）。
# 当前与 WRITING_WRITE_PATTERNS 同义，供各 domain 统一引用。
WORKSPACE_WRITE_PATTERNS = WRITING_WRITE_PATTERNS

__all__ = [
    "ArtifactPrerequisite",
    "ArtifactPrerequisiteMiddleware",
    "ArtifactValidationMiddleware",
    "ContextAssemblerMiddleware",
    "ErrorRecoveryMiddleware",
    "FilesystemPathGuardMiddleware",
    "FileWriteSerializeMiddleware",
    "TraceCallbackHandler",
    "TraceMiddleware",
    "WRITING_WRITE_PATTERNS",
    "WORKSPACE_WRITE_PATTERNS",
]
