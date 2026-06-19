"""domains.writing.middleware —— 写作专属中间件。

通用中间件（path_guard/error_recovery/trace/artifact 等）在 platform.agent.middleware。
本包只放强耦合写作语义的中间件：
  - GoalMiddleware — 写作目标约束
  - MetaReadOnlyMiddleware — 元数据只读保护
"""

from app.domains.writing.middleware.goal_middleware import GoalMiddleware
from app.domains.writing.middleware.meta_readonly_middleware import MetaReadOnlyMiddleware

__all__ = [
    "GoalMiddleware",
    "MetaReadOnlyMiddleware",
]
