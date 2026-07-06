"""EvolveContext —— 一次进化 session 的上下文（三功能解耦，决策 S5/S6 + D3/D6）。

进化 Agent 精简为「方案→执行」两阶段（T9/S3），ctx 同步精简：
删除 baseline/candidate/score/phase(6阶段)/eval_report_path/candidate_eval_path
等废弃字段；评估报告改为从 DB 加载到 eval_snapshot（S2）。

机制（沿用 D15）：contextvars 绑定，多 session 并发各取各的，互不串台。
与 eval_agent/ctx.py 的 EvaluationContext 独立，互不干扰。

字段（精简后）：
  - session_id / case_id               会话标识
  - trace_id                           进化输入的 trace（被改进对象）
  - eval_snapshot                      从 DB 加载的评估报告快照
  - design_doc_path / change_log_path  各阶段产出文档路径
  - review_status                      4 态机状态（S6）
  - recorder                           进化端 trace recorder（自观测，D6）
  - trace_id_self                      自观测 trace id（本次进化的录像）

emit_step/emit_log 改为代理到 recorder.append_business_event（D3：trace 统一接管）。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.trace.recorder import EvolutionTraceRecorder

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

        # D6：recorder 进 ctx，工具闭包通过 ctx 取。
        self.recorder: "EvolutionTraceRecorder | None" = None
        # 自观测 trace id（本次进化的录像）。由 api 启动时 create_run 设置。
        self.trace_id_self: str = ""

        # 进化输入（S2：trace + 评估报告，强前置 T2）
        self.trace_id: str = ""
        # 评估报告快照（从 evaluation_sessions 表加载，dict：scores/findings/report_md）
        self.eval_snapshot: dict[str, Any] = {}

        # 各阶段产出文档路径
        self.design_doc_path: str = ""
        self.change_log_path: str = ""

        # 4 态机状态（S6）：running / pending_review / published / discarded
        self.review_status: str = "running"

        # 配置层改动落地文件（apply_edits/validate_changes 读写点）。
        # 按 session 隔离：与 design_doc.md / change_log.md 同目录（docs.session_dir），
        # 避免多 session 并发互相覆盖（此前写到 workspace 根的全局共享 edits.json）。
        from app.evolve.docs import session_dir

        self._edits_path = session_dir(session_id) / "edits.json"

    # ── 事件推送便捷方法（D3：代理到 recorder.append_business_event）──

    def emit_step(self, tool: str, status: str, *, phase: str | None = None, **extra: Any) -> None:
        """推一个 step 事件。phase 指定阶段（plan/execute）。

        D3 改造：不再写 SessionEvents，改为 recorder.append_business_event。
        """
        if self.recorder and self.trace_id_self:
            self.recorder.append_business_event(
                self.trace_id_self, tool, status, phase=phase, **extra
            )

    def emit_log(self, message: str) -> None:
        if self.recorder and self.trace_id_self:
            self.recorder.append_business_event(
                self.trace_id_self, "log", "running", message=message
            )


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
