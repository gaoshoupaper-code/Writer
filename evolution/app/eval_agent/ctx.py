"""EvaluationContext —— 一次评估 session 的上下文（决策 S5）。

评估 Agent 独立于进化 Agent，自带专属 ctx。与 EvolveContext 完全解耦：
各自独立的 contextvar，互不串台（contextvar 天然按 asyncio Task 隔离）。

字段（轻量，只装评估所需）：
  - eval_id           评估 session id
  - trace_id          被评估的 trace（评估的唯一输入对象）
  - agent_version_type / agent_version_id   被评估 trace 对应的 Agent 版本（T7）
  - events            事件总线（SSE 推送评估进度）

评估产出不存 ctx（写入 evaluation_sessions 表，S2 DB 交接），所以 ctx 不需要
承载 scores/findings/report 等大字段——那些由 write_eval_report 工具直接落库。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.common import events as ev_events

# ── contextvar：当前协程上下文绑定的 EvaluationContext ──────────
# 与 evolve/ctx.py 的 _current_ctx 独立，互不干扰。
_current_eval_ctx: contextvars.ContextVar["EvaluationContext | None"] = contextvars.ContextVar(
    "eval_agent_current_ctx", default=None
)


class EvaluationContext:
    """工具间共享的评估 session 上下文（一次评估流程的载体）。

    每个评估 session 一个实例。评估工具闭包通过 get_eval_context() 取当前 ctx，
    实现 session 隔离的状态传递 + 事件推送。
    """

    def __init__(
        self,
        eval_id: str,
        trace_id: str,
        *,
        agent_version_type: str | None = None,
        agent_version_id: int | None = None,
    ) -> None:
        self.eval_id = eval_id
        self.trace_id = trace_id
        self.agent_version_type = agent_version_type
        self.agent_version_id = agent_version_id
        self.events: "ev_events.SessionEvents | None" = None  # 由 api 启动时注入

    # ── 事件推送便捷方法 ──

    def emit_step(self, tool: str, status: str, **extra: Any) -> None:
        """推一个 step 事件（评估工具调用）。"""
        if self.events:
            self.events.emit_step(tool, status, phase="eval", **extra)

    def emit_log(self, message: str) -> None:
        if self.events:
            self.events.emit_log(message)


# ── contextvar 读写 ─────────────────────────────────────────────


def set_eval_context(ctx: EvaluationContext) -> None:
    """绑定当前协程上下文的评估 ctx（评估 session 启动时调用）。"""
    _current_eval_ctx.set(ctx)


def get_eval_context() -> EvaluationContext | None:
    """取当前协程上下文绑定的评估 ctx。未设置返回 None。

    所有评估工具应通过此函数取 ctx。
    """
    return _current_eval_ctx.get()


__all__ = ["EvaluationContext", "set_eval_context", "get_eval_context"]
