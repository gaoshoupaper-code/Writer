"""EvaluationContext —— 一次评估 session 的上下文（决策 S5 + D3/D6）。

评估 Agent 独立于进化 Agent，自带专属 ctx。与 EvolveContext 完全解耦：
各自独立的 contextvar，互不串台（contextvar 天然按 asyncio Task 隔离）。

字段（轻量，只装评估所需）：
  - eval_id           评估 session id
  - input_trace_id    被评估的 trace（评估的唯一输入对象）
  - agent_version_type / agent_version_id   被评估 trace 对应的 Agent 版本（T7）
  - recorder          进化端 trace recorder（自观测，D6）
  - trace_id          自观测 trace id（本次评估的录像）

emit_step/emit_log 改为代理到 recorder.append_business_event（D3：trace 统一接管）。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.trace.recorder import EvolutionTraceRecorder

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
        # 被评估的 trace（评估输入对象）。原字段名 trace_id 保留向后兼容，
        # 但语义上是被评估对象，不是自观测 trace。
        self.input_trace_id = trace_id
        self.trace_id = trace_id  # 向后兼容别名（旧代码读 ctx.trace_id）
        self.agent_version_type = agent_version_type
        self.agent_version_id = agent_version_id
        # D6：recorder 进 ctx，工具闭包通过 ctx 取。
        self.recorder: "EvolutionTraceRecorder | None" = None
        # 自观测 trace id（本次评估的录像）。由 api 启动时 create_run 设置。
        self.trace_id_self: str = ""

    # ── 事件推送便捷方法（D3：代理到 recorder.append_business_event）──

    def emit_step(self, tool: str, status: str, **extra: Any) -> None:
        """推一个 step 事件（评估工具调用）。

        D3 改造：不再写 SessionEvents，改为 recorder.append_business_event。
        """
        if self.recorder and self.trace_id_self:
            self.recorder.append_business_event(
                self.trace_id_self, tool, status, phase="eval", **extra
            )

    def emit_log(self, message: str) -> None:
        """推一个 log 事件（Agent 思考/决策）。"""
        if self.recorder and self.trace_id_self:
            self.recorder.append_business_event(
                self.trace_id_self, "log", "running", phase="eval", message=message
            )


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
