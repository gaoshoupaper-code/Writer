"""记忆系统专用异常。

MemoryUnavailableError 是决策 10（失败即报错中断）的核心载体：
  - memory_recall middleware 在 before_model 阶段检测到图谱不可用时抛出
  - 上层 agent 执行捕获后中断当前写作流程

为什么单独定义而非用 RuntimeError：
  让上层能精确捕获"记忆不可用"这一类错误，与普通执行错误区分——
  未来可在 API 层返回专门的错误码（如 503 + 记忆系统不可用提示），
  而非笼统的 500。
"""


class MemoryUnavailableError(RuntimeError):
    """记忆系统不可用，按策略中断写作。

    触发场景：
      1. health_check 失败（FalkorDB 连接异常）
      2. 入图失败后 workspace 级 flag 标记不可用（.memory_unhealthy）
      3. 检索过程中 FalkorDB 突然断开
    """

    def __init__(self, reason: str, *, workspace_id: str | None = None) -> None:
        self.reason = reason
        self.workspace_id = workspace_id
        prefix = f"[workspace={workspace_id}] " if workspace_id else ""
        super().__init__(f"{prefix}记忆系统不可用：{reason}")


__all__ = ["MemoryUnavailableError"]
