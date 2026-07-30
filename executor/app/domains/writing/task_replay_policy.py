"""TaskReplayPolicy —— 平台级 task 防重放安全边界（CON-005 / FR-003 / DEC-005）。

根治 EVD-005 的"通用错误恢复把整个 task（子 Agent 委派）重放 N 次"：
  - ``task`` 工具运行一整个子 Agent，可能已写入产物；自动重放会重复副作用、
    重复计费，并制造约 18 分钟的乘法等待（3 × 3 × 120s）。
  - DEC-003：通用工具恢复不得因子 Agent 内部超时重放整个 task。
  - CON-005：该规则由当前 executor 平台运行时统一保证，覆盖普通创作、单次测试/A·B、
    风格优化、角色专家，以及 working / 当前发布 Harness；历史 snapshot 的源码、
    commit 与内容哈希保持不变。

落点设计（满足"平台注入，不改 harness 行为契约"）：
  - 本 Policy 定义在 executor 平台层，经 ``RuntimeContext.tool_replay_policy`` 注入。
  - harness 包的 ``ErrorRecoveryMiddleware.wrap_tool_call`` 在决定重试前调用
    ``policy.should_retry(tool_name, exc)``；``task``（及等价子 Agent 委派工具）恒为 False。
  - 历史加载的 snapshot 源码无法改动；其 ``assemble`` 仍会读到注入的 policy 字段
    （旧 assemble 不消费即按其原行为），但**当前发布与工作仓**的 ErrorRecovery 已统一
    服从本边界。平台在 run_snapshot 里记录 policy 版本以供审计。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("writer.task_replay_policy")

POLICY_VERSION = "task-replay-guard-v1"

# 会运行整个子 Agent 的委派工具名集合（DeepAgents 内建 + 项目等价名）。
# 这些工具的"重试"等于重跑整个子 Agent，必须由 Meta Agent 用新逻辑任务身份显式决策。
_NON_REPLAYABLE_TOOLS = frozenset({"task"})


class TaskReplayPolicy:
    """统一的 task 防重放判定。

    ``should_retry`` 返回 False 时，调用方（harness ErrorRecovery）必须：
      - 不重试该工具调用；
      - 把结构化错误交回 Meta Agent（让其用新 tool_call_id 显式重新委派）。
    返回 True 时表示该工具可按其既有重试预算安全重试。
    """

    def __init__(
        self,
        *,
        non_replayable: frozenset[str] = _NON_REPLAYABLE_TOOLS,
        on_replay_blocked: Callable[[str, str, BaseException], None] | None = None,
    ) -> None:
        self._non_replayable = non_replayable
        self._on_replay_blocked = on_replay_blocked

    def should_retry(self, tool_name: Any, exc: BaseException) -> bool:
        """判断某次工具调用失败后是否允许通用恢复重试。"""
        name = str(tool_name or "")
        if name in self._non_replayable:
            # 命中防重放：通知观测回调（写 Trace intervention），再返回 False。
            if self._on_replay_blocked is not None:
                try:
                    self._on_replay_blocked(name, POLICY_VERSION, exc)
                except Exception:  # noqa: BLE001 —— 观测回调失败不得阻断主流程
                    logger.debug("on_replay_blocked 回调失败", exc_info=True)
            return False
        return True

    @property
    def version(self) -> str:
        return POLICY_VERSION


__all__ = ["POLICY_VERSION", "TaskReplayPolicy"]
