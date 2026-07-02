"""evolve 事件总线 —— 进程内 session→事件队列映射（SSE 数据源）。

机制复用自旧 adapt/events.py（已删），但事件语义改为「进化 Agent 的工具调用步骤」。

  Agent 执行协程 ──emit──→ SessionEvents[session_id] ──→ SSE 端点消费

约束（单进程 + asyncio）：
  - 进程重启则进行中的 session 丢失（单轮手动 R8 已接受），队列也随之消失。
  - SSE 断连不重连：消费端断开，剩余事件被丢弃，最终报告落库后可从 DB 恢复。

事件类型：
  step    Agent 调用某工具的步骤（tool 名 + 状态 + 摘要）
  log     Agent 的自然语言输出（思考/决策）
  report  最终对比报告
  error   出错
  end     session 结束
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("evolution.common.events")

# 进程内全局注册表：session_id → 队列
_sessions: dict[str, "SessionEvents"] = {}
_registry_lock = asyncio.Lock()


class SessionEvents:
    """单个 evolve session 的事件队列封装。

    一个 session 一个实例。后台执行协程 emit 事件，SSE 端点消费。
    session 结束后保留最终状态，供迟到的查询。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # 有界队列：消费端慢/断开时不让生产端无限堆积爆内存。
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        # 终态：None=进行中 | "done" | "failed"
        self.terminal: str | None = None

    # ── 生产端（Agent 执行协程调用）──

    def emit(self, event: dict[str, Any]) -> None:
        """推一个事件到队列。队列满则丢弃最旧（fire-and-forget 实时流）。"""
        if self.terminal:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except Exception:
                pass

    def emit_step(self, tool: str, status: str, *, phase: str | None = None, **extra: Any) -> None:
        """便捷方法：推一个 step 事件。

        Args:
            tool:   工具/阶段名
            status: running / done / failed / blocked
            phase:  流水线阶段（D17：eval_baseline/plan/execute/run_candidate/
                    eval_candidate/report）。None 时不写入（向后兼容旧调用）。
        """
        event: dict[str, Any] = {"type": "step", "tool": tool, "status": status}
        if phase is not None:
            event["phase"] = phase
        event.update(extra)
        self.emit(event)

    def emit_log(self, message: str) -> None:
        """便捷方法：推一个 log 事件（Agent 思考/决策）。"""
        self.emit({"type": "log", "message": message})

    def emit_report(self, report: dict[str, Any]) -> None:
        """便捷方法：推最终报告事件。"""
        self.emit({"type": "report", "report": report})

    def finish(self, outcome: str, reason: str = "") -> None:
        """标记 session 终结。推 end/error 事件后封口。"""
        if self.terminal:
            return
        self.terminal = outcome
        if outcome == "failed":
            self.emit({"type": "error", "reason": reason})
        else:
            self.emit({"type": "end", "outcome": outcome, "reason": reason})


# ── 注册表操作 ───────────────────────────────────────────────


async def register(session_id: str) -> SessionEvents:
    """注册一个新 session（启动时调用）。"""
    async with _registry_lock:
        ev = SessionEvents(session_id)
        _sessions[session_id] = ev
        logger.info("事件总线：注册 session %s", session_id)
        return ev


def get(session_id: str) -> SessionEvents | None:
    """取 session 的事件队列（SSE 端点 + 查询用）。不存在返回 None。"""
    return _sessions.get(session_id)


def list_active() -> list[str]:
    """列出所有进行中的 session_id。"""
    return [sid for sid, ev in _sessions.items() if ev.terminal is None]


def list_all() -> list[str]:
    """列出所有注册过的 session_id（含已终结）。"""
    return list(_sessions.keys())


__all__ = [
    "SessionEvents",
    "register",
    "get",
    "list_active",
    "list_all",
]
