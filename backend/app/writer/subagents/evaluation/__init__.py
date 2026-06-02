"""统一评估子代理模块。

将 outline evaluation、detail outline evaluation、writing review
三个独立评估子代理合并为一个统一的评估工具（子子 agent），
由管道的 secondary_node 调用，传入文件路径参数。

公共 API：
  EvaluationType           — 评估类型枚举（outline / detail_outline / writing）
  build_evaluation_subagent — 统一评估子代理构建器
"""

from app.writer.subagents.evaluation.evaluation_subagent import (
    EvaluationType,
    build_evaluation_subagent,
)

__all__ = [
    "EvaluationType",
    "build_evaluation_subagent",
]
