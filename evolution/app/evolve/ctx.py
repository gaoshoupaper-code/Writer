"""EvolveContext —— 一次进化 session 的上下文（决策 D15）。

从 tools.py 迁出，独立成模块。核心改动（D15）：用 contextvars 替代模块级
ctx_global，支持多 session 并发 / 子代理嵌套委托时不串台。

机制说明：
  - 每个进化 session 启动时调 set_tool_context(ctx) 把当前 session 的 ctx
    绑定到 contextvar。
  - 所有工具（驱动器/评估/方案/执行子代理的工具）通过 get_tool_context()
    取"当前协程上下文"绑定的 ctx，而非全局唯一变量。
  - 多个 session 并发跑时，各自协程上下文有独立的 ctx，互不擦写。

向后兼容：tools.py 仍 re-export EvolveContext / set_tool_context，旧代码
`from app.evolve.tools import EvolveContext` 继续有效。
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.evolve import events as ev_events

# ── contextvar：当前协程上下文绑定的 EvolveContext ────────────────
# 默认未设置（None），set_tool_context 时绑定。contextvar 天然按
# asyncio Task / 线程隔离，多 session 并发各取各的，不会串台。
_current_ctx: contextvars.ContextVar["EvolveContext | None"] = contextvars.ContextVar(
    "evolve_current_ctx", default=None
)


class EvolveContext:
    """工具间共享的 session 上下文（一次进化流程的载体）。

    每个 session 一个实例。工具闭包通过 get_tool_context() 取当前 ctx，
    实现 session 隔离的状态传递 + 事件推送。
    """

    def __init__(self, session_id: str, case_id: str) -> None:
        self.session_id = session_id
        self.case_id = case_id
        self.events: "ev_events.SessionEvents | None" = None  # 由 api 启动时注入

        # 流程状态（工具间传递）
        self.baseline_trace: str = ""
        self.candidate_trace: str = ""
        self.baseline_score: float | None = None
        self.candidate_score: float | None = None
        self.report: dict[str, Any] = {}

        # 当前流水线阶段（D16/D-guard：6 阶段状态机）
        # eval_baseline → plan → execute → run_candidate → eval_candidate → report
        self.current_phase: str = ""

        # 各阶段产出文档路径（D16，落盘后存表）
        self.eval_report_path: str = ""
        self.design_doc_path: str = ""
        self.change_log_path: str = ""
        self.candidate_eval_path: str = ""

        # 当前 config（run_candidate 用 Agent 改后的）。
        # 改动检测：Agent 改完源码/edits 后，run_candidate 时重新 build config。
        self._edits_path = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "evolve_workspace" / "edits.json"
        )

    # ── 事件推送便捷方法 ──

    def emit_step(self, tool: str, status: str, *, phase: str | None = None, **extra: Any) -> None:
        """推一个 step 事件。phase 为 None 时用 ctx.current_phase（D17）。"""
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
