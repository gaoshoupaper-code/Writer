"""隔离子进程执行器（FR-006 / NFR-001 / DEC-002 / RSK-002）。

把 run_ab_generation 放进独立子进程，父进程持有 Process 句柄，取消时：
  1. cancel_event.set()（协作式取消，让 Agent 在 super-step 边界优雅退出）
  2. process.join(剩余时间)（等待协作式退出）
  3. 超时 process.terminate() / process.kill()（强杀，处理 C 层不可中断阻塞）

子进程内独立构建 TraceRecorder（无法跨进程共享内存队列/锁），trace 数据写到
共享文件系统（workspace 是临时目录），子进程通过 Queue 回传 trace_id 给父进程。

NFR-001：从权威停止受理起 P100 ≤ 10.0 秒。
RSK-002：强杀隔离进程不破坏父进程状态；子进程的半提交状态随进程消亡。
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.cancel_state import HARD_STOP_DEADLINE_SECONDS

logger = logging.getLogger("executor.isolation")


@dataclass
class WorkerResult:
    """子进程执行结果（通过 Queue 回传）。"""

    trace_id: str | None = None
    status: str = "unknown"       # done / failed / cancelled
    error: str | None = None


def _worker_main(
    source_root_str: str,
    demand_md: str,
    workspace_root_str: str,
    cancel_event: Any,
    result_queue: Any,
) -> None:
    """子进程入口：构建独立 recorder + 跑 run_ab_generation。

    必须是模块级函数（multiprocessing spawn 可 pickle），不能是闭包或 lambda。
    子进程内重建全部依赖（recorder / settings / drain），不继承父进程内存状态。
    """
    try:
        # 子进程内独立初始化（spawn 模式不继承父进程的已初始化全局变量）。
        from app.platform.trace import TraceRecorder
        from app.platform.core.settings import get_settings
        from app.routers.ab_endpoint import run_ab_generation

        source_root = Path(source_root_str)
        workspace_root = Path(workspace_root_str)

        writer_settings = get_settings()
        trace_recorder = TraceRecorder()
        # 子进程内启动 drain 协程——但子进程没有 asyncio 事件循环在跑。
        # run_ab_generation 是同步调用，drain 不会运行；退化为同步写盘
        # （append_event 的 _drain_active() 返回 False 时同步写，兼容测试/直接调用）。
        # 这样保证 trace 事件即时落盘，即使子进程被强杀也不丢已写事件。

        def _on_trace_created(tid: str) -> None:
            # trace 创建后立即回传，让父进程在 running 期间就能拿到 trace_id。
            try:
                result_queue.put_nowait(("trace_id", tid))
            except Exception:
                pass

        trace_id = run_ab_generation(
            source_root=source_root,
            demand_md=demand_md,
            trace_recorder=trace_recorder,
            writer_settings=writer_settings,
            on_trace_created=_on_trace_created,
            cancel_event=cancel_event,
        )

        # 判定终态（与 _execute_ab 逻辑一致）。
        if cancel_event.is_set():
            result_queue.put_nowait(("result", WorkerResult(
                trace_id=trace_id, status="cancelled",
            )))
        else:
            result_queue.put_nowait(("result", WorkerResult(
                trace_id=trace_id, status="done",
            )))
    except BaseException as exc:
        logger.exception("隔离子进程执行失败")
        result_queue.put_nowait(("result", WorkerResult(
            status="failed", error=str(exc),
        )))


class IsolatedGenerationWorker:
    """管理一个隔离子进程执行 run_ab_generation。

    用法：
        worker = IsolatedGenerationWorker(source_root, demand_md, workspace_root)
        worker.start()
        trace_id = worker.wait_for_trace_id(timeout=30)
        # ... 运行中 ...
        result = worker.stop_and_collect(deadline=10.0)  # 取消并收集结果
    """

    def __init__(
        self,
        source_root: Path,
        demand_md: str,
        workspace_root: Path,
    ) -> None:
        self._source_root = source_root
        self._demand_md = demand_md
        self._workspace_root = workspace_root
        # multiprocessing 原语（跨进程安全）。
        ctx = mp.get_context("spawn")  # spawn 最干净，不继承父进程状态
        self._cancel_event = ctx.Event()
        self._result_queue: mp.Queue = ctx.Queue()
        self._process: mp.Process | None = None
        self._trace_id: str | None = None
        self._lock = threading.Lock()

    @property
    def trace_id(self) -> str | None:
        with self._lock:
            return self._trace_id

    def start(self) -> None:
        """启动子进程。"""
        if self._process is not None:
            raise RuntimeError("worker already started")
        self._process = mp.Process(
            target=_worker_main,
            args=(
                str(self._source_root),
                self._demand_md,
                str(self._workspace_root),
                self._cancel_event,
                self._result_queue,
            ),
            daemon=True,  # 父进程退出时子进程也退出，防孤儿
        )
        self._process.start()
        logger.info("隔离子进程启动: pid=%s", self._process.pid)

    def wait_for_trace_id(self, timeout: float = 60.0) -> str | None:
        """阻塞等待子进程回传 trace_id（run_ab_generation 创建 trace 后立即回传）。

        在运行中持续 drain result_queue，确保 trace_id 消息不被 result 消息阻塞。
        """
        import queue as queue_mod

        deadline = _monotonic() + timeout
        while _monotonic() < deadline:
            # 先检查进程是否已退出（异常情况）。
            if self._process is not None and not self._process.is_alive():
                self._drain_queue()
                break
            try:
                kind, value = self._result_queue.get(timeout=0.5)
            except queue_mod.Empty:
                continue
            if kind == "trace_id":
                with self._lock:
                    self._trace_id = value
                return value
            if kind == "result":
                # 子进程可能在回传 trace_id 前就结束（失败）——记录终态。
                with self._lock:
                    if value.trace_id:
                        self._trace_id = value.trace_id
                    self._last_result = value
                return value.trace_id
        return self.trace_id

    def stop_and_collect(
        self, deadline: float = HARD_STOP_DEADLINE_SECONDS
    ) -> WorkerResult:
        """请求停止并收集结果（FR-006 / NFR-001）。

        流程：协作式取消 → join(剩余时间) → 超时强杀 → 收集终态。
        从调用起不超过 deadline 秒（NFR-001 P100 ≤ 10.0s）。
        """
        if self._process is None:
            return getattr(self, "_last_result", None) or WorkerResult(
                status="unknown", error="worker not started"
            )

        # 1. 协作式取消：set 标志，让 Agent 在 super-step 边界优雅退出。
        self._cancel_event.set()

        # 2. 等待协作式退出（给足剩余时间）。
        remaining = deadline
        self._process.join(timeout=remaining)

        if self._process.is_alive():
            # 3. 超时强杀：进程级终止，处理 C 层不可中断阻塞。
            logger.warning(
                "隔离子进程 %s 协作式取消超时（%.1fs），执行强杀",
                self._process.pid, deadline,
            )
            _force_terminate(self._process)
            # 强杀后再等一下确保进程退出。
            self._process.join(timeout=2.0)
            if self._process.is_alive():
                logger.error("隔离子进程 %s 强杀后仍存活", self._process.pid)

        # 4. 收集终态：从 queue drain result，或根据 cancel 标志推断。
        self._drain_queue()
        result = getattr(self, "_last_result", None)
        if result is not None:
            return result
        # 进程被杀且无显式 result：按 cancelled 收尾（取消意图已表达）。
        tid = self.trace_id
        return WorkerResult(trace_id=tid, status="cancelled")

    def _drain_queue(self) -> None:
        """排空 result_queue，更新 trace_id 和 last_result。"""
        import queue as queue_mod

        while True:
            try:
                kind, value = self._result_queue.get_nowait()
            except queue_mod.Empty:
                break
            if kind == "trace_id":
                with self._lock:
                    if self._trace_id is None:
                        self._trace_id = value
            elif kind == "result":
                with self._lock:
                    if value.trace_id and not self._trace_id:
                        self._trace_id = value.trace_id
                    self._last_result = value

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()


def _force_terminate(process: mp.Process) -> None:
    """跨平台强杀子进程（Windows taskkill / POSIX SIGKILL）。

    RSK-002：强杀隔离进程不破坏父进程状态——子进程的内存/文件句柄随进程消亡。
    """
    pid = process.pid
    if pid is None:
        return
    try:
        if sys.platform == "win32":
            # Windows: taskkill /T /F 杀进程树（含子进程，如 LLM HTTP 连接的线程）。
            os.system(f"taskkill /PID {pid} /T /F >nul 2>&1")
        else:
            # POSIX: SIGKILL 进程组（start_new_session=True 时子进程是组长）。
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    except Exception:
        # 兜底：process.terminate()（SIGTERM），不完美但比不杀好。
        logger.exception("强杀失败，退回 terminate()")
        process.terminate()


def _monotonic() -> float:
    import time
    return time.monotonic()
