"""EvolveContext —— 一次进化 session 的上下文（对话式共创工作台，决策 T1.3）。

进化 Agent 从「一气呵成跑完」重构为「对话式共创 → 拍板 → 落地」三段式：
  running      探查阶段（Agent 读评估+探查要素，自动跑完）
  conversing   对话共创（用户与 Agent 多轮对话，浮现进化点）
  finalizing   落地阶段（拍板后一次性编码+validate+change_log）
  pending_review / published / discarded / failed / cancelled（终态）

ctx 是工具间共享的运行时上下文。新增字段（决策 T1.3）：
  - session_status     6+态机状态的运行时缓存（FlowGuard 据此拦截落地工具）
  - thread_id          LangGraph checkpoint 的 thread_id（= session_id，决策 T1）

EvolveMessagesRepo / EvolvePointsRepo 为静态方法类，Agent 工具直接 import
使用，不放 ctx（无实例状态可注入）。

机制（沿用 D15）：contextvars 绑定，多 session 并发各取各的，互不串台。
与 eval_agent/ctx.py 的 EvaluationContext 独立，互不干扰。

字段：
  - session_id / case_id               会话标识
  - trace_id                           进化输入的 trace（被改进对象）
  - eval_snapshot                      从 DB 加载的评估报告快照
  - design_doc_path / change_log_path  各阶段产出文档路径
  - session_status                     6+态机状态缓存（决策 T1.3）
  - thread_id                          LangGraph thread_id（= session_id，决策 T1）
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


# ── 状态机常量（决策 T1.4）────────────────────────────────────
# session_status 取值。DB 列是 TEXT 无约束，这里集中定义供代码引用。
# 状态流转：
#   running ─→ conversing ─→ finalizing ─→ pending_review ─→ published / discarded
#                                    ↓        ↓
#                                 failed   failed
# 任何 running/conversing/finalizing 状态可被 stop 推进到 cancelled。
STATUS_RUNNING = "running"               # 探查阶段（Agent 自动读评估+探查要素）
STATUS_CONVERSING = "conversing"         # 对话共创（用户与 Agent 多轮对话）
STATUS_FINALIZING = "finalizing"         # 落地阶段（拍板后一次性编码）
STATUS_PENDING_REVIEW = "pending_review" # 待审（落地完成，等用户发布/丢弃）
STATUS_PUBLISHED = "published"           # 已发版（终态）
STATUS_DISCARDED = "discarded"           # 已丢弃（终态，working 区已 reset）
STATUS_FAILED = "failed"                 # 失败（落地失败，等用户丢弃清理）
STATUS_CANCELLED = "cancelled"           # 已取消（用户主动停止）

# 占用 working 区的活跃状态——同时只允许一个（决策 G 单会话锁）。
ACTIVE_STATUSES = frozenset({
    STATUS_RUNNING, STATUS_CONVERSING, STATUS_FINALIZING, STATUS_PENDING_REVIEW,
})

# 终态——不可再变更。
TERMINAL_STATUSES = frozenset({
    STATUS_PUBLISHED, STATUS_DISCARDED, STATUS_CANCELLED,
})


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

        # 数据闭环 F1：trace 所属的数据集层（golden|growing）。
        # golden → 验证模式（不能退化）；growing → 探索模式（找新方向）。
        # 从 manual_tests.origin_layer 推导（evolve_start 时查），None=未知（非测试 trace）。
        self.origin_layer: str | None = None

        # 各阶段产出文档路径
        self.design_doc_path: str = ""
        self.change_log_path: str = ""

        # 对话式共创（决策 T1.3）：
        # session_status 是 6+态机状态的运行时缓存。FlowGuard 中间件据此判断
        # 当前阶段、决定是否拦截落地工具（决策 T9）。Agent 工具只读不写——
        # status 推进权在 API 端点（防止 Agent 越权自催落地）。
        self.session_status: str = STATUS_RUNNING

        # LangGraph thread_id（= session_id，决策 T1）。每轮 ainvoke 通过它从
        # checkpoint 恢复完整对话史。设为单独字段方便未来与 session_id 解耦。
        self.thread_id: str = session_id

    def reload_session_status(self) -> str:
        """从 DB 同步当前 session_status。

        在 API 端点切换 status 后调用，保证 ctx 缓存与 DB 一致。
        Agent 工具不调用此方法（status 变更权属于 API 层）。
        """
        from app.evolve import db as ev_db
        session = ev_db.get_session(self.session_id)
        if session:
            self.session_status = session.get("status") or STATUS_RUNNING
        return self.session_status

    # ── 事件推送便捷方法（D3：代理到 recorder.append_business_event）──

    def emit_step(self, tool: str, status: str, **extra: Any) -> None:
        """推一个 step 事件。

        D3 改造：不再写 SessionEvents，改为 recorder.append_business_event。
        单体化后无阶段概念，不再接受 phase 参数。
        """
        if self.recorder and self.trace_id_self:
            self.recorder.append_business_event(
                self.trace_id_self, tool, status, **extra
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


__all__ = [
    "EvolveContext",
    "set_tool_context",
    "get_tool_context",
    # 状态机常量
    "STATUS_RUNNING",
    "STATUS_CONVERSING",
    "STATUS_FINALIZING",
    "STATUS_PENDING_REVIEW",
    "STATUS_PUBLISHED",
    "STATUS_DISCARDED",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
]
