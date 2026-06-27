"""adapt 事件总线 —— 进程内 session→事件队列映射（Phase 8，前端 SSE 数据源）。

adapt loop 是后台异步跑的（决策 E4a 同步执行），前端要看实时进度。
LangGraph 的 astream 能流式吐 state，但那是 graph 调用方的局部迭代器，
HTTP/SSE 端点拿不到。这里做一个进程内的「事件总线」做桥接：

  graph 执行协程 ──put──→ SessionEvents[session_id] ───→ SSE 端点消费
                              （asyncio.Queue per session）

约束（与需求 D10/D4 对齐）：
  - 进程重启则进行中的 session 丢失（A12a 已接受），队列也随之消失。
  - SSE 断连不重连（D10）：消费端断开，队列里剩余事件被丢弃，session 状态仍由
    后台 graph 推进，轮级结果照常落库，前端刷新可从 adapt_rounds 恢复。
  - 单进程模型：不跨进程，不做持久化（热数据，进程内存够用）。

事件类型（前端订阅，6 类，见需求 §4.3）：
  node_start / node_output / node_end / round_end / session_end / error
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

logger = logging.getLogger("evolution.adapt.events")

# 进程内全局注册表：session_id → 队列
# 用 plain dict 而非 "manager"，因为单进程 + asyncio，无需跨进程同步。
_sessions: dict[str, "SessionEvents"] = {}
_registry_lock = asyncio.Lock()


class SessionEvents:
    """单个 adapt session 的事件队列封装。

    一个 session 一个实例。后台执行协程 put 事件，SSE 端点 get 事件。
    session 结束后保留最终状态（completed/terminated/error），供迟到的查询。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # 有界队列：消费端慢/断开时不让生产端无限堆积爆内存。
        # 满了就丢弃最旧事件（实时进度本来就是"看当前"，历史可从 DB 恢复）。
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        # 终态：None=进行中 | "completed" | "terminated" | "error"
        self.terminal: str | None = None
        self.terminal_reason: str = ""

    # ── 生产端（graph 执行协程调用）──

    def emit(self, event: dict[str, Any]) -> None:
        """推一个事件到队列。队列满则丢弃最旧（fire-and-forget 实时流）。"""
        if self.terminal:
            # 已终结的 session 不再推事件（避免误导）
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # 满：丢最旧，保最新（实时流优先当前态）
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except Exception:
                pass

    def finish(self, outcome: str, reason: str = "") -> None:
        """标记 session 终结。推一个 session_end / error 事件后封口。"""
        if self.terminal:
            return
        self.terminal = outcome
        self.terminal_reason = reason
        if outcome == "error":
            self.emit({"type": "error", "reason": reason})
        else:
            self.emit({"type": "session_end", "outcome": outcome, "reason": reason})


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
    """列出所有进行中的 session_id（terminal is None）。"""
    return [sid for sid, ev in _sessions.items() if ev.terminal is None]


def list_all() -> list[str]:
    """列出所有注册过的 session_id（含已终结）。"""
    return list(_sessions.keys())


def request_stop(session_id: str) -> bool:
    """请求软停（D12）。设置标志位，loop_control 下轮检查时终止。
    返回是否成功设置（session 不存在或已终结则 False）。
    """
    ev = _sessions.get(session_id)
    if ev is None or ev.terminal is not None:
        return False
    ev.stop_requested = True  # type: ignore[attr-defined]
    logger.info("事件总线：session %s 收到软停请求", session_id)
    return True


def is_stop_requested(session_id: str) -> bool:
    """loop_control 查询：是否被请求软停。"""
    ev = _sessions.get(session_id)
    if ev is None:
        return False
    return getattr(ev, "stop_requested", False)


__all__ = [
    "SessionEvents",
    "register",
    "get",
    "list_active",
    "list_all",
    "request_stop",
    "is_stop_requested",
]
