"""EvolveContext —— 一次进化 session 的上下文（三功能解耦，决策 S5/S6）。

进化 Agent 精简为「方案→执行」两阶段（T9/S3），ctx 同步精简：
删除 baseline/candidate/score/phase(6阶段)/eval_report_path/candidate_eval_path
等废弃字段；评估报告改为从 DB 加载到 eval_snapshot（S2）。

机制（沿用 D15）：contextvars 绑定，多 session 并发各取各的，互不串台。
与 eval_agent/ctx.py 的 EvaluationContext 独立，互不干扰。

字段（精简后）：
  - session_id / case_id / events       会话标识 + 事件总线
  - trace_id                            进化输入的 trace（被改进对象）
  - eval_snapshot                       从 DB 加载的评估报告快照（dict，含 scores/findings/report_md）
  - design_doc_path / change_log_path   各阶段产出文档路径
  - review_status                       4 态机状态（S6，与 DB status 同步）
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.common import events as ev_events

# ── contextvar：当前协程上下文绑定的 EvolveContext ────────────────
# 与 eval_agent/ctx.py 的 _current_eval_ctx 独立。
_current_ctx: contextvars.ContextVar["EvolveContext | None"] = contextvars.ContextVar(
    "evolve_current_ctx", default=None
)


class EvolveContext:
    """工具间共享的进化 session 上下文（一次进化流程的载体）。

    每个 session 一个实例。工具闭包通过 get_tool_context() 取当前 ctx，
    实现 session 隔离的状态传递 + 事件推送。
    """

    def __init__(self, session_id: str, case_id: str = "") -> None:
        self.session_id = session_id
        self.case_id = case_id
        self.events: "ev_events.SessionEvents | None" = None  # 由 api 启动时注入

        # 进化输入（S2：trace + 评估报告，强前置 T2）
        self.trace_id: str = ""
        # 评估报告快照（从 evaluation_sessions 表加载，dict：scores/findings/report_md）
        self.eval_snapshot: dict[str, Any] = {}

        # 各阶段产出文档路径
        self.design_doc_path: str = ""
        self.change_log_path: str = ""

        # 4 态机状态（S6）：running / pending_review / published / discarded
        self.review_status: str = "running"

        # 进化工作目录（edits/change_log 存放点，沿用旧路径）
        self._workspace = (
            Path(__file__).resolve().parent.parent.parent / "data" / "evolve_workspace"
        )

    # ── 事件推送便捷方法 ──

    def emit_step(self, tool: str, status: str, *, phase: str | None = None, **extra: Any) -> None:
        """推一个 step 事件。phase 指定阶段（plan/execute）。"""
        if self.events:
            self.events.emit_step(tool, status, phase=phase, **extra)

    def emit_log(self, message: str) -> None:
        if self.events:
            self.events.emit_log(message)


# ── contextvar 读写 ─────────────────────────────────────────────


def set_tool_context(ctx: EvolveContext) -> None:
    """绑定当前协程上下文的 ctx（每次进化流程启动时调用）。

    使用 contextvar.set 而非全局赋值，保证多 session 并发隔离（D15）。
    """
    _current_ctx.set(ctx)


def get_tool_context() -> EvolveContext | None:
    """取当前协程上下文绑定的 ctx。未设置返回 None。

    所有工具应通过此函数取 ctx，而非引用模块级变量。
    """
    return _current_ctx.get()


__all__ = ["EvolveContext", "set_tool_context", "get_tool_context"]
