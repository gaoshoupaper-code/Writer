# ==============================================================================
# 中间件模块（middleware）
#
# 本模块集中导出所有代理中间件，供上层 meta_agent 和各子代理按需组装。
#
# 中间件在 DeepAgents 框架中以洋葱模型包裹代理的核心执行流程：
#   用户请求 → [外层中间件] → [内层中间件] → 模型调用 / 工具调用 → 逐层返回
#
# 各中间件职责一览：
#   ArtifactPrerequisiteMiddleware  — 子代理执行前校验前置产物文件是否存在且非空
#   ContextAssemblerMiddleware      — 通用上下文组装：由主代理配置文件路径，新阶段时读取并注入
#   ErrorRecoveryMiddleware         — 工具调用出错时自动重试，耗尽后注入恢复建议
#   FilesystemPathGuardMiddleware   — 拦截非法文件写入路径，防止越权或路径穿越
#   GoalMiddleware                  — 注册目标工具和状态 schema，拦截未达标的最终输出
#   TraceMiddleware                 — 记录模型调用和工具调用的开始/完成/错误事件
#   TraceCallbackHandler            — LangChain 回调处理器，注册 run 层级的父子关系
# ==============================================================================

from app.writer.middleware.artifact_prerequisite_middleware import ArtifactPrerequisite, ArtifactPrerequisiteMiddleware
from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.middleware.error_recovery_middleware import ErrorRecoveryMiddleware
from app.writer.middleware.goal_middleware import GoalMiddleware
from app.writer.middleware.path_guard_middleware import FilesystemPathGuardMiddleware
from app.writer.middleware.trace_callback import TraceCallbackHandler
from app.writer.middleware.trace_middleware import TraceMiddleware

__all__ = [
    "ArtifactPrerequisite",
    "ArtifactPrerequisiteMiddleware",
    "ContextAssemblerMiddleware",
    "ErrorRecoveryMiddleware",
    "FilesystemPathGuardMiddleware",
    "GoalMiddleware",
    "TraceCallbackHandler",
    "TraceMiddleware",
]
