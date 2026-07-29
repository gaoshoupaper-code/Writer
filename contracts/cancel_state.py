"""统一取消状态契约（FR-006/007/008, CON-003, DEC-002/005）。

跨 executor 与 evolution 共享的权威取消状态字典、单调终态规则和取消身份定义。

设计依据：需求 .claude/md/20260728_182303（FR-006/007, NFR-001, CON-003, DEC-002/005）。

核心不变量（CON-003）：
  - 终态单调：cancelled / completed / done / failed 一旦按合法竞态规则提交，
    不得被迟到写入改成另一终态。
  - 用户取消不是失败：cancelled 不得计入失败率或被映射为 failed。
  - 取消身份（cancel_id）贯穿产物 / Trace / usage / 下游隔离，保证幂等收敛。
"""

from __future__ import annotations

from enum import Enum


class CancelState(str, Enum):
    """取消状态机的权威状态字典（FR-006, DEC-002/008）。

    状态流转：pending → running → cancelling → cancelled | cancel_timeout
      - pending：任务已建但 worker/执行尚未登记（EDGE-005 取消早于 worker 建立时起点）。
      - cancelling：用户提交停止后、硬终止确认前的可见中间态（立即反馈，DEC-002）。
      - cancelled：确认全部收敛后的终态。
      - cancel_timeout：受理后 10.0 秒仍无法确认全部收敛的诚实终态（EDGE-004），
        不得伪装成 cancelled；仅可在后台取得真实停止证明后前进为 cancelled。
    """

    PENDING = "pending"              # 任务已建、执行尚未登记（可被取消请求直接命中）
    RUNNING = "running"
    CANCELLING = "cancelling"        # 已受理停止，正在硬终止（立即可见中间态）
    CANCELLED = "cancelled"          # 确认全部收敛后的终态
    CANCEL_TIMEOUT = "cancel_timeout"  # 10s 内无法确认收敛，诚实告警态


# 业务终态集合（CON-003 单调性保护对象）。
# 一旦进入这些状态，不得被迟到写入（轮询/摄入/执行结果）覆写为另一终态。
TERMINAL_STATES = frozenset({
    "completed", "done", "failed", "cancelled", "cancel_timeout",
    "interrupted",  # 心跳超时/进程重启的中断态（非用户取消，但也是终态）
})

# 取消类终态：用户主动取消产生的终态（不含 failed/interrupted）。
CANCEL_TERMINAL_STATES = frozenset({"cancelled", "cancel_timeout"})

# 十秒硬终止时限（NFR-001，DEC-005：从权威停止服务受理起算）。
HARD_STOP_DEADLINE_SECONDS = 10.0


def is_terminal(status: str | None) -> bool:
    """判断状态是否为不可逆终态（CON-003 单调性检查用）。"""
    return status in TERMINAL_STATES


def is_cancel_terminal(status: str | None) -> bool:
    """判断状态是否为取消类终态（区分用户取消 vs 失败，CON-003）。"""
    return status in CANCEL_TERMINAL_STATES


def can_transition_to(current: str | None, target: str) -> bool:
    """单调终态规则：终态不可被覆写为另一终态（CON-003, EDGE-002）。

    合法竞态规则：
      - 非终态 → 任何状态：允许（running → cancelling → cancelled 等）。
      - 终态 → 同一终态：允许（幂等）。
      - 终态 → 另一终态：拒绝（cancelled 不被改 failed，done 不被改 cancelled）。
      - cancel_timeout → cancelled：允许（后台恢复确认终止后转正，EDGE-004 恢复路径）。

    用户取消不得被迟到完成或摄入改写为 failed/done（EVD-006 根因）。
    """
    if not is_terminal(current):
        return True
    if current == target:
        return True
    # cancel_timeout 是可恢复的：后台确认终止后可转 cancelled。
    if current == "cancel_timeout" and target == "cancelled":
        return True
    return False


def canonical_status(raw: str | None) -> str:
    """对外统一状态字典（FR-007/CON-009 兼容性）。

    混合版本窗口里旧客户端/旧读取路径遇到新增的 cancelling/cancel_timeout/pending 时，
    必须得到可理解的非成功状态——既不崩溃，也不误报成功或把取消超时当完成（CON-009）。
    内部 done/completed 命名保留，对外统一为 completed。
    """
    if raw is None:
        return "unknown"
    # done / completed 统一为 completed（内部可保留旧名，对外统一）。
    if raw in ("done", "completed"):
        return "completed"
    return raw


__all__ = [
    "CancelState",
    "TERMINAL_STATES",
    "CANCEL_TERMINAL_STATES",
    "HARD_STOP_DEADLINE_SECONDS",
    "is_terminal",
    "is_cancel_terminal",
    "can_transition_to",
    "canonical_status",
]
