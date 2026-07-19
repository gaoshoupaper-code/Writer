from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import UUID, uuid4

from app.schemas.screenplay import ThreadSummary
from app.platform.trace.increment import IncrementState, compute_increment
from app.platform.trace.projector import TraceProjector
from app.platform.trace.schemas import TraceDetail, TraceLogEvent, TraceRunSummary
from app.platform.trace.summary_export import export_trace_summary

# ── 写盘解耦参数 ──────────────────────────────────────────────
# 根因：append_event 原来在事件循环线程上同步 open("a")+write，单次生成会写
# 上千个事件（每个几十 KB，trace jsonl 膨胀到 39MB），密集同步 IO 周期性占满
# 事件循环，导致生成期间所有别的 HTTP 请求（轮询/删除/health）全部排队超时。
# 修复：append_event 只把待写行 append 进 _pending_writes（deque，微秒级、
# 纯内存、不占事件循环），由后台 drain 协程成批落盘（to_thread，不占事件循环）。
# trace 结束（complete_run/fail_run）时同步 flush 该 trace 残余事件保证完整。
_DRAIN_INTERVAL = 0.5  # 后台 drain 协程每 0.5s 成批写一次
_FLUSH_BATCH_MAX = 200  # 单批最多写多少行（避免一批太大又阻塞线程池 worker）

# evolution 通知：evolution_notify_url 配置后，trace 结束时 POST 通知它摄入。
# 用懒 import + 短超时，任何失败都静默降级（evolution 兜底扫描会补漏通知的 trace）。
_EVOLUTION_NOTIFY_TIMEOUT = 2.0

# HITL cancelled 收尾的来源类型（D5：状态统一 cancelled，error 字段区分来源）。
CancelReason = Literal["user_stop", "client_disconnect", "timeout"]
_CANCEL_REASON_MESSAGES: dict[str, str] = {
    "user_stop": "User stopped",
    "client_disconnect": "Stream cancelled (client disconnected)",
    "timeout": "Awaiting input timeout (2h)",
}

# 僵尸清理：awaiting_input 超 2h 未 resume → cancelled（需求决策）。
# 扫描间隔 5min（2h 阈值，无需高频）。
_ZOMBIE_TIMEOUT_SEC = 2 * 60 * 60  # 2h
_ZOMBIE_SCAN_INTERVAL = 5 * 60     # 5min


@dataclass
class TraceRunHandle:
    trace_id: str
    queue: asyncio.Queue[TraceLogEvent] = field(default_factory=asyncio.Queue)


class TraceRecorder:
    def __init__(self) -> None:
        self._locks: dict[str, RLock] = {}
        self._sequences: dict[str, int] = {}
        self._queues: dict[str, asyncio.Queue[TraceLogEvent]] = {}
        self._started_monotonic: dict[str, float] = {}
        self._run_paths: dict[str, Path] = {}
        self._parents: dict[str, str | None] = {}
        self._run_kinds: dict[str, str] = {}
        self._run_names: dict[str, str] = {}
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._projector = TraceProjector()
        # 增量存储状态（Phase 1 T1/T4/T5/T6）：每个活跃 trace 一个 IncrementState，
        # 维护"已见消息"索引用于计算 LLM input 的增量范围。
        # 跨重启丢失（T6 降级）：丢失后该 trace 退化为全量存储，不报错。
        self._increment_states: dict[str, IncrementState] = {}
        # T15 活跃大盘：记录活跃 trace 的 endpoint（轻量，不读文件）。
        self._run_endpoints: dict[str, str] = {}
        # Phase 3：trace_id → workspace_path 持久索引。
        # 在 create_run 时登记，不随 _cleanup_run_state 清除（evolution 在 trace
        # 完成后才来拉取，那时活跃态已清，需靠此索引反查 workspace）。
        # 进程重启后丢失——退化为 scan 端点列表扫描兜底。
        self._trace_workspace: dict[str, str] = {}
        # anchor 计数器：为每条事件分配稳定 anchor_id（T1）。
        # 格式 anchor-{trace_seq}-{global_counter}，写进 jsonl 后永久稳定。
        self._anchor_counter: int = 0
        # 写盘解耦：append_event 只 append 进此缓冲区，后台 drain 协程成批落盘。
        # deque + Lock 而非 asyncio.Queue：append_event 的调用者含同步回调
        # (TraceCallbackHandler run_inline=True)，不能 await，必须跨线程安全。
        self._pending_writes: deque[tuple[Path, str]] = deque()
        self._pending_lock = RLock()
        self._drain_task: asyncio.Task[None] | None = None
        # 僵尸清理后台任务（D：扫 awaiting_input 超 2h 的 trace → cancelled）
        self._zombie_task: asyncio.Task[None] | None = None
        # HITL 停止标记（D6）：trace_id → 用户是否点了"停止"按钮。
        # 区分"用户主动停止"（走 cancel reason=user_stop）与"连接断开"
        # （走 cancel reason=client_disconnect）。cancel 收尾时清除。
        self._user_stop_requested: dict[str, bool] = {}
        # trace_id → SSE 生成器 task 注册表（D-停止真生效）。
        # _user_stop_requested 只是标志位，本身不停止执行——真正终止靠前端 abort SSE
        # 触发 CancelledError。但浏览器刷新/关闭/cloudflared 掐断后前端 abortController
        # 丢失，那个还在后台跑的 trace 就再也停不掉。此注册表让 POST /stop 能跨请求
        # task.cancel()，把 CancelledError 主动注入生成器，走原 except 三路分流。
        # generate_stream 入口 create_run 后登记（register_run_task），
        # finally 清理（unregister_run_task），_cleanup_run_state 兜底。
        self._run_tasks: dict[str, asyncio.Task] = {}

    def create_run(self, thread: ThreadSummary, endpoint: str, run_purpose: str = "user_generation") -> TraceRunHandle:
        trace_id = f"trace-{uuid4().hex}"
        started_at = datetime.now(UTC)
        run_path = self._trace_path(thread, trace_id, started_at)
        run_path.parent.mkdir(parents=True, exist_ok=True)

        self._locks[trace_id] = RLock()
        self._sequences[trace_id] = 0
        self._queues[trace_id] = asyncio.Queue()
        self._started_monotonic[trace_id] = time.perf_counter()
        self._run_paths[trace_id] = run_path
        self._increment_states[trace_id] = IncrementState()
        self._run_endpoints[trace_id] = endpoint
        self._trace_workspace[trace_id] = thread.workspace_path

        summary = TraceRunSummary(
            trace_id=trace_id,
            workspace_id=thread.workspace_id,
            thread_id=thread.thread_id,
            session_name=thread.session_name,
            workspace_path=thread.workspace_path,
            endpoint=endpoint,
            status="running",
            started_at=started_at.isoformat(),
            path=self._relative_trace_path(thread, trace_id, started_at),
        )
        self._write_run_index(thread, summary)
        handle = TraceRunHandle(trace_id=trace_id, queue=self._queues[trace_id])
        self.append_event(
            trace_id,
            {
                "type": "run_start",
                "status": "running",
                "source": "system",
                "input": {
                    "endpoint": endpoint,
                    "thread_id": thread.thread_id,
                    "workspace_id": thread.workspace_id,
                    "session_name": thread.session_name,
                    # 归属键（D2/D20）：trace 自带 user_id，evolution 摄入时提取。
                    "user_id": thread.user_id,
                    # 防自指断路预留（D12 约束）：区分用户生成 vs 优化执行。
                    # 后期优化闭环只处理 user_generation，optimization 的 trace
                    # 摄入但不进优化池（等价 langfuse env 标记断路）。
                    "run_purpose": run_purpose,
                },
            },
        )
        return handle

    def resume_run(self, thread: ThreadSummary, trace_id: str) -> tuple[TraceRunHandle, bool]:
        """续接活跃 trace（HITL resume 缝合点3）。

        三条路径（D10 双键恢复）：
        1. 内存活跃（_queues 命中）→ 复用，is_new=False（不发 run_start）。
        2. 内存丢失但 index 命中 awaiting_input → 重建最小活跃态，is_new=False
           （执行端重启后仍能 resume 同一条 trace，丙方案）。
        3. 都不命中 → 降级 create_run，is_new=True（发 run_start）。← D2=A

        resume 后状态变迁：awaiting_input → running。复用/重建路径都要把 index
        改回 running 并 notify evolution（否则 index 永远停在 awaiting_input，
        前端/进化端看不到 trace 已恢复执行）。
        """
        # 路径1：内存命中（正常情况）
        if trace_id in self._queues:
            self._update_index_status(thread, trace_id, "running", None)
            _notify_evolution(thread, trace_id, "running", None)
            return TraceRunHandle(trace_id=trace_id, queue=self._queues[trace_id]), False
        # 路径2：index 命中 awaiting_input → 重建（执行端重启后）
        run = self.find_run_by_trace_id(trace_id)
        if run is not None and run.status == "awaiting_input":
            handle = self._rebuild_active_state(thread, run)
            self._update_index_status(thread, trace_id, "running", None)
            _notify_evolution(thread, trace_id, "running", None)
            return handle, False
        # 路径3：降级开新 trace（兜底）
        return self.create_run(thread, "screenplay.generate.stream"), True

    def _rebuild_active_state(
        self, thread: ThreadSummary, run: TraceRunSummary
    ) -> TraceRunHandle:
        """从 index run summary 重建最小活跃态（D10，丙方案）。

        执行端重启后内存活跃态丢失，但 index.json + jsonl + langgraph checkpoint
        都还在。此方法重建 recorder 的记账状态让 append_event 能继续工作：
        queue/lock/seq/path/endpoints/workspace（_increment_states 不重建→降级全量）。
        """
        trace_id = run.trace_id
        run_path = Path(thread.workspace_path) / run.path
        # event_count = last_seq（正常情况两者相等，D10.乙），seq 从此续接
        last_seq = run.event_count
        self._locks[trace_id] = RLock()
        self._sequences[trace_id] = last_seq
        self._queues[trace_id] = asyncio.Queue()
        # duration 诊断值会不准（perf_counter 重置），可接受
        self._started_monotonic[trace_id] = time.perf_counter()
        self._run_paths[trace_id] = run_path
        # _increment_states 不重建 → 退化为全量存储（recorder 既有降级设计）
        self._run_endpoints[trace_id] = run.endpoint
        self._trace_workspace[trace_id] = thread.workspace_path
        return TraceRunHandle(trace_id=trace_id, queue=self._queues[trace_id])

    def register_run(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        kind: str,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        key = str(run_id)
        self._parents[key] = str(parent_run_id) if parent_run_id else None
        self._run_kinds[key] = kind
        if name:
            self._run_names[key] = name
        if metadata:
            self._run_metadata[key] = metadata

    def run_parent(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._parents.get(str(run_id))

    def run_kind(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._run_kinds.get(str(run_id))

    def run_name(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._run_names.get(str(run_id))

    def run_metadata(self, run_id: UUID | str | None) -> dict[str, Any] | None:
        if run_id is None:
            return None
        return self._run_metadata.get(str(run_id))

    def append_event(self, trace_id: str, values: dict[str, Any]) -> TraceLogEvent:
        lock = self._lock_for(trace_id)
        with lock:
            sequence = self._sequences.get(trace_id)
            if sequence is None:
                raise KeyError(f"Trace run is not active: {trace_id}")
            sequence += 1
            self._sequences[trace_id] = sequence

            event_type = str(values["type"])

            # ── 增量存储（Phase 1 T1/T4/T5/T8）──
            # 为每条事件分配稳定 anchor_id（T1），写进 jsonl 永久稳定。
            # 对 llm_start 计算 input 增量：与前次公共前缀引用为 range，
            # input 只存新增尾部；range 为空 = 全量（T8）。
            self._anchor_counter += 1
            output_anchor_id = f"anchor-{trace_id}-{sequence}-{self._anchor_counter}"

            input_value = values.get("input")
            input_context_range = values.get("input_context_range")
            if event_type == "llm_start" and input_value is not None:
                inc_state = self._increment_states.get(trace_id)
                if inc_state is not None:
                    result = compute_increment(inc_state, input_value, output_anchor_id)
                    input_value = result.input_to_store
                    input_context_range = result.input_context_range
                # inc_state 为 None（跨重启降级 T6）→ 不增量，input 保持全量。

            event = TraceLogEvent(
                trace_id=trace_id,
                event_id=str(values.get("event_id") or f"{trace_id}-{sequence}"),
                sequence=sequence,
                type=values["type"],
                status=values["status"],
                timestamp=str(values.get("timestamp") or self._now()),
                source=values["source"],
                duration_ms=values.get("duration_ms"),
                run_id=self._optional_str(values.get("run_id")),
                parent_run_id=self._optional_str(values.get("parent_run_id")),
                parent_event_id=self._optional_str(values.get("parent_event_id")),
                agent_name=self._optional_str(values.get("agent_name")),
                node_name=self._optional_str(values.get("node_name")),
                model_name=self._optional_str(values.get("model_name")),
                input=input_value,
                output=_sanitize_tool_call_inputs(values.get("output")),
                usage=values.get("usage"),
                tool_calls=_tool_calls_payload(values.get("tool_calls")),
                tool_call_id=self._optional_str(values.get("tool_call_id")),
                tool_name=self._optional_str(values.get("tool_name")),
                tool_args=_preserve_deliverable_args(
                    values.get("tool_name"), values.get("tool_args")
                ),
                tool_output=_sanitize_tool_call_inputs(values.get("tool_output")),
                output_anchor_id=output_anchor_id,
                input_context_range=input_context_range,
                error=self._optional_str(values.get("error")),
            )

            run_path = self._run_paths.get(trace_id)
            if run_path is None:
                raise KeyError(f"Trace path is not registered: {trace_id}")
            # 写盘解耦：drain 协程运行时（生产 lifespan 场景）只入内存缓冲区，
            # 后台成批落盘，不占事件循环；drain 未运行时（测试/未走 lifespan 的
            # 直接调用）退回同步写，保证 append 后立即可读——兼容现有调用契约。
            json_line = event.model_dump_json(exclude_none=True)
            if self._drain_active():
                with self._pending_lock:
                    self._pending_writes.append((run_path, f"{json_line}\n"))
            else:
                try:
                    with run_path.open("a", encoding="utf-8") as file:
                        file.write(f"{json_line}\n")
                except OSError:
                    pass

            queue = self._queues.get(trace_id)
            if queue is not None:
                queue.put_nowait(event)
            return event

    def _drain_active(self) -> bool:
        """drain 协程是否在运行（用于 append_event 选择异步/同步写盘路径）。"""
        return self._drain_task is not None and not self._drain_task.done()

    # ── HITL：awaiting_input 状态 + 停止标记 ──────────────────────

    def await_input_run(self, thread: ThreadSummary, trace_id: str) -> TraceLogEvent:
        """标记 trace 进入 awaiting_input（HITL interrupt，非终态）。

        agent 调 ask_user → interrupt() 暂停图后由 domain 层调用（D1）。
        写 run_awaiting 事件 + 更新 index status=awaiting_input + flush 落盘 +
        notify evolution（状态变迁即推，R 方案）。

        与 complete/cancel/fail 的关键区别：**不清内存活跃态**——agent 仍挂在
        interrupt 点，等待 Command(resume) 续接。state 已由 langgraph checkpoint
        落盘，recorder 只记录"等待中"这一状态变迁。
        """
        event = self.append_event(
            trace_id,
            {
                "type": "run_awaiting",
                "status": "awaiting_input",
                "source": "system",
            },
        )
        # 更新 index 状态（不走 _finalize_run——那是终态收尾，会清内存）
        self._update_index_status(thread, trace_id, "awaiting_input", None)
        self.flush_sync(trace_id)
        # 状态变迁通知 evolution（awaiting_input 也摄入，己方案）
        _notify_evolution(thread, trace_id, "awaiting_input", None)
        return event

    def is_awaiting_input(self, trace_id: str) -> bool:
        """trace 是否处于 awaiting_input 状态。

        优先读内存（活跃态还在则最快）；内存丢了读 index（执行端重启后）。
        agent.generate_stream 的 CancelledError 分支用它判断"连接断开时是否
        在 interrupt 期间"——是则保持 awaiting_input 不收尾（D4）。
        """
        # 内存活跃态：从最近的 run_awaiting 事件推断（_sequences 在 = 还活跃）
        if trace_id in self._sequences:
            # 活跃 trace，查 index 确认当前态（避免读到 resume 后的 running）
            run = self.find_run_by_trace_id(trace_id)
            return run is not None and run.status == "awaiting_input"
        return False

    def request_user_stop(self, trace_id: str) -> None:
        """标记用户点了停止按钮（D6 停止信号）。

        POST /stop 端点调用。cancel_run 收尾时读此标记区分 user_stop /
        client_disconnect，并清除标记。
        """
        self._user_stop_requested[trace_id] = True

    def is_user_stop_requested(self, trace_id: str) -> bool:
        """用户是否请求了停止（D6）。CancelledError 分支读它分流收尾。"""
        return self._user_stop_requested.get(trace_id, False)

    def register_run_task(self, trace_id: str, task: asyncio.Task) -> None:
        """登记 SSE 生成器 task（D-停止真生效）。

        generate_stream 在 create_run 之后调用，把当前 asyncio.Task 注册进来。
        POST /stop 据此 task.cancel() 主动注入 CancelledError，不再依赖前端
        abort SSE 连接——浏览器刷新/cloudflared 掐断后仍能停止后台执行。
        幂等：重复登记覆盖旧引用（同一 trace 不会并发跑两个 task）。
        """
        self._run_tasks[trace_id] = task

    def unregister_run_task(self, trace_id: str) -> None:
        """清理 task 注册（generate_stream finally 调，幂等）。"""
        self._run_tasks.pop(trace_id, None)

    def cancel_run_task(self, trace_id: str) -> bool:
        """主动 cancel SSE 生成器 task（POST /stop 调）。

        返回是否命中并发出 cancel：trace 已结束/未登记返回 False，不抛错。
        task.cancel() 把 CancelledError 注入生成器，走 generate_stream 的
        except asyncio.CancelledError 三路分流（user_stop/awaiting_input/
        client_disconnect），收尾成 cancelled 终态。与 _user_stop_requested
        标志位配合：标志位决定 reason 文案，task.cancel 决定真的停止生效。
        """
        task = self._run_tasks.get(trace_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def complete_run(self, thread: ThreadSummary, trace_id: str) -> TraceLogEvent:
        duration_ms = self._duration_ms(trace_id)
        event = self.append_event(
            trace_id,
            {
                "type": "run_end",
                "status": "completed",
                "source": "system",
                "duration_ms": duration_ms,
            },
        )
        self._finalize_run(thread, trace_id, "completed", duration_ms, None)
        return event

    def fail_run(self, thread: ThreadSummary, trace_id: str, error: BaseException) -> TraceLogEvent:
        return self._fail_run(thread, trace_id, f"{error.__class__.__name__}: {error}")

    def cancel_run(
        self,
        thread: ThreadSummary,
        trace_id: str,
        reason: CancelReason = "client_disconnect",
    ) -> TraceLogEvent:
        """标记 trace 为 cancelled（终态）。

        cancelled 表示"执行未完成"——区别于 failed（agent 报错）。三种来源：
        - user_stop：用户点了停止按钮（配合 _user_stop_requested 标记，D6）
        - client_disconnect：SSE 连接断开（cloudflared/Cloudflare 超时、网络抖动）
        - timeout：awaiting_input 超 2h 未响应（僵尸清理）
        收尾时清除停止标记。
        """
        error_message = _CANCEL_REASON_MESSAGES.get(reason, "Cancelled")
        # 清除停止标记（无论是否命中，收尾后都不再需要）
        self._user_stop_requested.pop(trace_id, None)
        return self._cancel_run(thread, trace_id, error_message)

    def _cancel_run(self, thread: ThreadSummary, trace_id: str, error_message: str) -> TraceLogEvent:
        """写 run_cancelled 事件并收尾（终态 cancelled）。"""
        duration_ms = self._duration_ms(trace_id)
        event = self.append_event(
            trace_id,
            {
                "type": "run_cancelled",
                "status": "cancelled",
                "source": "system",
                "duration_ms": duration_ms,
                "error": error_message,
            },
        )
        self._finalize_run(thread, trace_id, "cancelled", duration_ms, error_message)
        return event

    def _fail_run(self, thread: ThreadSummary, trace_id: str, error_message: str) -> TraceLogEvent:
        duration_ms = self._duration_ms(trace_id)
        event = self.append_event(
            trace_id,
            {
                "type": "run_error",
                "status": "failed",
                "source": "system",
                "duration_ms": duration_ms,
                "error": error_message,
            },
        )
        self._finalize_run(thread, trace_id, "failed", duration_ms, error_message)
        return event

    # ── 写盘解耦：后台 drain + 同步 flush ────────────────────────

    def start_drain(self) -> None:
        """启动后台 drain 协程（由应用 lifespan 调用）。

        幂等：重复调用不会创建多个 task。必须在事件循环线程内调用
        （asyncio.get_event_loop 创建 task 依赖运行中的 loop）。
        """
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop())

    def start_zombie_scanner(self) -> None:
        """启动僵尸清理后台任务（由应用 lifespan 调用，幂等）。

        扫 index 中 status=awaiting_input 且超 _ZOMBIE_TIMEOUT_SEC 未 resume 的 trace，
        标记为 cancelled（reason=timeout）+ notify evolution。
        """
        if self._zombie_task is None or self._zombie_task.done():
            self._zombie_task = asyncio.create_task(self._zombie_scan_loop())

    async def aclose(self) -> None:
        """关闭：停止 drain/僵尸扫描协程，并把残余事件全部落盘（由 lifespan shutdown 调用）。"""
        if self._drain_task is not None:
            self._drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None
        if self._zombie_task is not None:
            self._zombie_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._zombie_task
            self._zombie_task = None
        # 停 drain 后再同步刷一次，保证进程退出前所有事件落盘。
        self._flush_all_sync()

    async def _drain_loop(self) -> None:
        """后台循环：周期性成批把缓冲区事件写盘。

        每轮 await sleep（让出事件循环）→ 取出一批 → to_thread 写盘（不占事件循环）。
        单批上限 _FLUSH_BATCH_MAX，避免一批太大又把线程池 worker 占住太久。
        """
        while True:
            await asyncio.sleep(_DRAIN_INTERVAL)
            batch = self._take_pending_batch()
            if batch:
                await asyncio.to_thread(self._write_batch_sync, batch)

    async def _zombie_scan_loop(self) -> None:
        """后台僵尸清理循环：周期扫 awaiting_input 超时的 trace → cancelled。"""
        while True:
            await asyncio.sleep(_ZOMBIE_SCAN_INTERVAL)
            try:
                await asyncio.to_thread(self._zombie_scan_once)
            except Exception:
                # 扫描异常不应拖垮后台任务，静默继续下一轮
                pass

    def _zombie_scan_once(self) -> int:
        """扫描一次 awaiting_input 超时 trace，标记为 cancelled。

        遍历 _trace_workspace 索引，读每个 workspace 的 index.json，
        找 status=awaiting_input 且 run_awaiting 事件距今超 _ZOMBIE_TIMEOUT_SEC 的 trace。
        由于 index 不存"何时进入 awaiting"（_finalize_run 才记时间），这里从
        jsonl 读最后一条 run_awaiting 事件的时间戳判断超时。

        返回清理的数量。
        """
        now = datetime.now(UTC)
        count = 0
        seen_ws: set[str] = set()
        # 复制 _trace_workspace 的 items 避免扫描中字典变动
        for trace_id, ws_path in list(self._trace_workspace.items()):
            if ws_path in seen_ws:
                continue
            seen_ws.add(ws_path)
            index_path = Path(ws_path) / "traces" / "index.json"
            if not index_path.exists():
                continue
            try:
                with index_path.open("r", encoding="utf-8") as f:
                    runs = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(runs, dict):
                continue
            for tid, run_data in runs.items():
                if run_data.get("status") != "awaiting_input":
                    continue
                # 从 jsonl 读最后一条 run_awaiting 时间戳判断超时
                run_path = Path(ws_path) / str(run_data.get("path", ""))
                if not run_path.exists():
                    continue
                awaiting_ts = self._last_awaiting_timestamp(run_path)
                if awaiting_ts is None:
                    continue
                try:
                    elapsed = (now - datetime.fromisoformat(awaiting_ts)).total_seconds()
                except (ValueError, TypeError):
                    continue
                if elapsed < _ZOMBIE_TIMEOUT_SEC:
                    continue
                # 超时 → cancelled。重建 thread 摘要从 index 取信息。
                self._cancel_zombie(tid, run_data, ws_path)
                count += 1
        return count

    def _last_awaiting_timestamp(self, trace_path: Path) -> str | None:
        """从 jsonl 读最后一条 run_awaiting 事件的时间戳（倒序找）。"""
        try:
            # 倒序读，找最后一条 run_awaiting
            lines = trace_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "run_awaiting":
                    return data.get("timestamp")
            return None
        except OSError:
            return None

    def _cancel_zombie(self, trace_id: str, run_data: dict[str, Any], ws_path: str) -> None:
        """将僵尸 awaiting_input trace 标记为 cancelled（超时）。"""
        # 构造最小 ThreadSummary（cancel_run/_finalize_run 需要 thread 定位 index）
        from app.schemas.screenplay import ThreadSummary
        thread = ThreadSummary(
            thread_id=str(run_data.get("thread_id", "")),
            workspace_id=str(run_data.get("workspace_id", "")),
            session_name=str(run_data.get("session_name", "")),
            workspace_path=ws_path,
            created_at=run_data.get("started_at", ""),
            updated_at=run_data.get("started_at", ""),
        )
        # 若内存活跃态还在（罕见：同进程长时间未 resume），走正常 cancel 路径收尾
        if trace_id in self._sequences:
            self._cancel_run(thread, trace_id, _CANCEL_REASON_MESSAGES["timeout"])
        else:
            # 内存已无（常见）：直接写 run_cancelled 事件 + 更新 index + notify
            self._cancel_zombie_no_memory(thread, trace_id)

    def _cancel_zombie_no_memory(self, thread: ThreadSummary, trace_id: str) -> None:
        """内存活跃态已丢失的僵尸 trace 收尾（直接写事件 + 更新 index）。"""
        # 临时重建最小活跃态让 append_event 工作
        run = self.find_run_by_trace_id(trace_id)
        if run is None:
            return
        run_path = Path(thread.workspace_path) / run.path
        self._locks[trace_id] = RLock()
        self._sequences[trace_id] = run.event_count
        self._run_paths[trace_id] = run_path
        try:
            self._cancel_run(thread, trace_id, _CANCEL_REASON_MESSAGES["timeout"])
        finally:
            # _cancel_run → _finalize_run 已清内存，兜底再清一次
            self._locks.pop(trace_id, None)
            self._sequences.pop(trace_id, None)
            self._run_paths.pop(trace_id, None)

    def _take_pending_batch(self) -> list[tuple[Path, str]]:
        """从缓冲区取出一批待写行（线程安全，最多 _FLUSH_BATCH_MAX 条）。"""
        with self._pending_lock:
            count = min(len(self._pending_writes), _FLUSH_BATCH_MAX)
            if count == 0:
                return []
            batch = [self._pending_writes.popleft() for _ in range(count)]
        return batch

    def _write_batch_sync(self, batch: list[tuple[Path, str]]) -> None:
        """同步写一批事件：按 run_path 分组，每组一次打开（减少 open 次数）。

        此方法在 to_thread 的线程池 worker 里执行，不占事件循环。
        """
        grouped: dict[Path, list[str]] = {}
        for run_path, line in batch:
            grouped.setdefault(run_path, []).append(line)
        for run_path, lines in grouped.items():
            try:
                with run_path.open("a", encoding="utf-8") as file:
                    file.writelines(lines)
            except OSError:
                # 写盘失败不应中断 drain（trace 是派生数据，不可拖垮主流程）。
                # 与 generate_storyline_graph 的容错策略一致。
                pass

    def flush_sync(self, trace_id: str) -> None:
        """同步刷掉指定 trace 的残余事件（trace 结束时调用，保证数据完整）。

        仅 complete_run/fail_run 路径调用——此时该 trace 已停止产生事件，
        同步开销可接受；且确保后续 read_run 能读到完整的 jsonl。
        """
        run_path = self._run_paths.get(trace_id)
        if run_path is None:
            return
        pending: list[str] = []
        with self._pending_lock:
            # 过滤出本 trace 的待写行，其余放回队首（保持顺序）。
            rest: list[tuple[Path, str]] = []
            while self._pending_writes:
                path, line = self._pending_writes.popleft()
                if path == run_path:
                    pending.append(line)
                else:
                    rest.append((path, line))
            self._pending_writes.extendleft(reversed(rest))
        if pending:
            try:
                with run_path.open("a", encoding="utf-8") as file:
                    file.writelines(pending)
            except OSError:
                pass

    def _flush_all_sync(self) -> None:
        """同步刷掉所有残余事件（仅 aclose 关闭时调用）。"""
        with self._pending_lock:
            batch = list(self._pending_writes)
            self._pending_writes.clear()
        if batch:
            self._write_batch_sync(batch)

    def list_runs(self, thread: ThreadSummary) -> list[TraceRunSummary]:
        runs = self._read_run_index(thread)
        summaries = [TraceRunSummary.model_validate(run) for run in runs.values() if run["thread_id"] == thread.thread_id]
        return sorted(summaries, key=lambda run: run.started_at, reverse=True)

    def read_run(self, thread: ThreadSummary, trace_id: str) -> TraceDetail | None:
        return self._read_run_detail(thread, trace_id)

    def find_run_by_trace_id(self, trace_id: str) -> TraceRunSummary | None:
        """按 trace_id 反查 run summary（不依赖 thread，Phase 3 evolution 拉取用）。

        先查内存 _trace_workspace 索引拿 workspace_path，再从该 workspace 的
        index.json 读 run summary。进程重启后索引丢失则返回 None（由 scan 兜底）。

        返回 TraceRunSummary（含相对 workspace 的 path 字段，可拼出 jsonl 全路径）。
        """
        ws_path = self._trace_workspace.get(trace_id)
        if ws_path is None:
            return None
        index_path = Path(ws_path) / "traces" / "index.json"
        if not index_path.exists():
            return None
        try:
            with index_path.open("r", encoding="utf-8") as f:
                runs = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        run_data = runs.get(trace_id) if isinstance(runs, dict) else None
        if run_data is None:
            return None
        return TraceRunSummary.model_validate(run_data)

    def read_trace_events(self, trace_id: str, since_seq: int = 0) -> list[TraceLogEvent] | None:
        """按 trace_id 读取 trace 的事件列表（Phase 3 evolution 拉取用）。

        since_seq（D8 增量）：只返回 sequence > since_seq 的事件。0 = 全量。
        依赖 _trace_workspace 索引定位 workspace，再从 run summary 的 path 拼 jsonl。
        """
        run = self.find_run_by_trace_id(trace_id)
        if run is None:
            return None
        ws_path = self._trace_workspace.get(trace_id)
        if ws_path is None:
            return None
        trace_path = Path(ws_path) / run.path
        if not trace_path.exists():
            return None
        events = self._read_events(trace_path)
        if since_seq > 0:
            events = [e for e in events if e.sequence > since_seq]
        return events

    def list_recent_runs(self, since_iso: str = "") -> list[dict[str, Any]]:
        """列出近期完成的 trace（Phase 3 evolution scan 兜底用）。

        遍历 _trace_workspace 索引，读每个 workspace 的 index.json，
        返回 started_at >= since_iso 的 run summary 列表。
        进程重启后索引不全——此方法只覆盖本进程生命周期内创建的 trace。
        """
        result: list[dict[str, Any]] = []
        # 按 workspace 分组读 index（避免重复读同一 index）
        seen_ws: set[str] = set()
        for trace_id, ws_path in self._trace_workspace.items():
            if ws_path in seen_ws:
                continue
            seen_ws.add(ws_path)
            index_path = Path(ws_path) / "traces" / "index.json"
            if not index_path.exists():
                continue
            try:
                with index_path.open("r", encoding="utf-8") as f:
                    runs = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(runs, dict):
                continue
            for tid, run_data in runs.items():
                started = str(run_data.get("started_at", ""))
                if since_iso and started < since_iso:
                    continue
                result.append({
                    "trace_id": tid,
                    "workspace_id": run_data.get("workspace_id", ""),
                    "status": run_data.get("status", ""),
                    "started_at": started,
                    "ended_at": run_data.get("ended_at"),
                })
        return result

    def delete_run(self, thread: ThreadSummary, trace_id: str) -> bool:
        runs = self._read_run_index(thread)
        run = runs.get(trace_id)
        if run is None or run.get("thread_id") != thread.thread_id:
            return False
        self._delete_run_from_index(thread, runs, trace_id, run)
        self._write_index(thread, runs)
        return True

    def delete_thread_runs(self, thread: ThreadSummary) -> int:
        runs = self._read_run_index(thread)
        thread_runs = [
            (trace_id, run)
            for trace_id, run in runs.items()
            if run.get("thread_id") == thread.thread_id
        ]
        for trace_id, run in thread_runs:
            self._delete_run_from_index(thread, runs, trace_id, run)
        self._write_index(thread, runs)
        return len(thread_runs)

    def read_run_snapshot(self, thread: ThreadSummary, trace_id: str) -> TraceDetail | None:
        return self._read_run_detail(thread, trace_id)

    def get_active_queue(self, trace_id: str) -> asyncio.Queue[TraceLogEvent] | None:
        return self._queues.get(trace_id)

    def list_active_runs(self) -> list[dict[str, Any]]:
        """列出当前活跃 trace（T15 活跃大盘，只读、轻量）。

        返回内存中正在运行的 trace 摘要，供 evolution 轮询展示活跃大盘。
        不读文件、不持久化，纯内存读取。
        status 字段（awaiting_input/running）从 index 读——活跃态只有这两种，
        终态(completed/cancelled/failed) 的 trace 已被 _finalize_run 清出内存。
        """
        now = time.perf_counter()
        result: list[dict[str, Any]] = []
        for trace_id, started in self._started_monotonic.items():
            run = self.find_run_by_trace_id(trace_id)
            status = run.status if run else "running"
            result.append({
                "trace_id": trace_id,
                "endpoint": self._run_endpoints.get(trace_id, ""),
                "duration_ms": int((now - started) * 1000),
                "event_count": self._sequences.get(trace_id, 0),
                "status": status,
            })
        return result

    def set_prompt_version(self, trace_id: str, prompt_name: str, version: int) -> None:
        """记录本次 trace 使用的 prompt 版本（T13：版本进 trace）。

        agent 构建 prompt 后调用，版本号写入 run 级 metadata，供后期 badcase
        回放对照"旧版本失败 vs 新版本成功"。第一期只记录主控 prompt。
        """
        meta = self._run_metadata.setdefault(trace_id, {})
        meta.setdefault("prompt_versions", {})[prompt_name] = version

    def _read_run_detail(self, thread: ThreadSummary, trace_id: str) -> TraceDetail | None:
        run_data = self._read_run_index(thread).get(trace_id)
        if run_data is None:
            return None
        run = TraceRunSummary.model_validate(run_data)
        trace_path = Path(thread.workspace_path) / run.path
        if not trace_path.exists():
            raise FileNotFoundError(f"Trace file is missing: {trace_path}")
        events = self._read_events(trace_path)
        if run.status == "running":
            run.event_count = len(events)
        projection = self._projector.project(run, events)
        return TraceDetail(
            run=run,
            events=events,
            nodes=projection.nodes,
            context=projection.context,
            todos=projection.todos,
        )

    def _read_events(self, trace_path: Path) -> list[TraceLogEvent]:
        events_by_id: dict[str, TraceLogEvent] = {}
        with trace_path.open("r", encoding="utf-8") as file:
            for line in file:
                stripped = line.strip()
                if not stripped:
                    continue
                event_data = json.loads(stripped)
                if event_data.get("type") == "run_link" and event_data.get("source") == "callback":
                    continue
                event = TraceLogEvent.model_validate(_sanitize_event_data(event_data))
                events_by_id.setdefault(event.event_id, event)
        return sorted(events_by_id.values(), key=lambda event: (event.timestamp, event.sequence))

    def _update_index_status(
        self,
        thread: ThreadSummary,
        trace_id: str,
        status: str,
        error: str | None,
    ) -> None:
        """更新 index.json 的 trace 状态（非终态变更用，如 awaiting_input）。

        与 _finalize_run 的区别：不清内存、不设 ended_at/duration_ms、不导出摘要、
        不发终态通知。仅更新 status + event_count（保证监测面板/evolution 看到最新态）。
        """
        runs = self._read_run_index(thread)
        run = runs.get(trace_id)
        if run is None:
            return  # index 没有则无法更新（不应发生，静默）
        run["status"] = status
        run["event_count"] = self._sequences.get(trace_id, run.get("event_count", 0))
        if error is not None:
            run["error"] = error
        runs[trace_id] = run
        self._write_index(thread, runs)

    def _finalize_run(
        self,
        thread: ThreadSummary,
        trace_id: str,
        status: str,
        duration_ms: int,
        error: str | None,
    ) -> None:
        # trace 结束：先把该 trace 残余事件同步落盘，再更新 index/导出摘要/清理内存。
        # 此时该 trace 已停止产生事件，同步 flush 开销可接受且保证数据完整。
        self.flush_sync(trace_id)

        # T13：把本次 trace 使用的 prompt 版本记录为 run_meta 事件（持久化到 jsonl，
        # 供 evolution 摄入 + 后期 badcase 回放对照）。在 cleanup 前读 _run_metadata。
        prompt_versions = (self._run_metadata.get(trace_id) or {}).get("prompt_versions")
        if prompt_versions:
            self.append_event(
                trace_id,
                {
                    "type": "run_meta",
                    "status": status,
                    "source": "system",
                    "input": {"prompt_versions": prompt_versions},
                },
            )
            self.flush_sync(trace_id)  # 确保 run_meta 事件落盘

        runs = self._read_run_index(thread)
        run = runs.get(trace_id)
        if run is None:
            raise KeyError(f"Trace run not found in index: {trace_id}")
        run["status"] = status
        run["ended_at"] = self._now()
        run["duration_ms"] = duration_ms
        run["event_count"] = self._sequences[trace_id]
        run["error"] = error
        runs[trace_id] = run
        self._write_index(thread, runs)

        # 导出模型可读的执行记录摘要（包括失败的 trace）
        self._export_summary(thread, trace_id, run)

        self._cleanup_run_state(trace_id)

        # 所有 trace 收尾完成后，通知监测服务摄入。
        # 放最后：即使通知失败也不影响 trace 正常结束（evolution 兜底扫描补漏）。
        _notify_evolution(thread, trace_id, status, run)

    def _export_summary(
        self,
        thread: ThreadSummary,
        trace_id: str,
        run_data: dict[str, Any],
    ) -> None:
        """trace 结束后自动导出摘要 JSON。"""
        try:
            trace_path = Path(thread.workspace_path) / str(run_data["path"])
            if not trace_path.exists():
                return
            events = self._read_events(trace_path)
            run_summary = TraceRunSummary.model_validate(run_data)
            projection = self._projector.project(run_summary, events)
            detail = TraceDetail(
                run=run_summary,
                events=events,
                nodes=projection.nodes,
                context=projection.context,
                todos=projection.todos,
            )
            summary_path = trace_path.with_name(f"{trace_id}_summary.json")
            export_trace_summary(detail, summary_path)
        except Exception:
            # 摘要导出失败不应影响 trace 正常结束流程
            pass

    def _delete_run_from_index(
        self,
        thread: ThreadSummary,
        runs: dict[str, dict[str, Any]],
        trace_id: str,
        run: dict[str, Any],
    ) -> None:
        if run.get("status") == "running" or trace_id in self._queues or trace_id in self._run_paths:
            raise ValueError("Trace is still running")

        trace_path = Path(thread.workspace_path) / str(run["path"])
        if trace_path.exists():
            trace_path.unlink()
        # 同时清理摘要文件
        summary_path = trace_path.with_name(f"{trace_id}_summary.json")
        if summary_path.exists():
            summary_path.unlink()
        del runs[trace_id]
        self._cleanup_run_state(trace_id)

    def _cleanup_run_state(self, trace_id: str) -> None:
        self._queues.pop(trace_id, None)
        self._started_monotonic.pop(trace_id, None)
        self._run_paths.pop(trace_id, None)
        self._locks.pop(trace_id, None)
        self._sequences.pop(trace_id, None)
        self._increment_states.pop(trace_id, None)
        self._run_endpoints.pop(trace_id, None)
        # task 注册兜底清理：正常路径 generate_stream finally 已 unregister，
        # 这里是极端兜底（finally 未执行 / 进程内异常路径）。
        self._run_tasks.pop(trace_id, None)

    def _read_run_index(self, thread: ThreadSummary) -> dict[str, dict[str, Any]]:
        index_path = self._index_path(thread)
        if not index_path.exists():
            return {}
        with index_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid trace index format: {index_path}")
        return data

    def _write_run_index(self, thread: ThreadSummary, run: TraceRunSummary) -> None:
        runs = self._read_run_index(thread)
        runs[run.trace_id] = run.model_dump()
        self._write_index(thread, runs)

    def _write_index(self, thread: ThreadSummary, runs: dict[str, dict[str, Any]]) -> None:
        index_path = self._index_path(thread)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("w", encoding="utf-8") as file:
            json.dump(runs, file, ensure_ascii=False, indent=2)

    def _index_path(self, thread: ThreadSummary) -> Path:
        return Path(thread.workspace_path) / "traces" / "index.json"

    def _trace_path(self, thread: ThreadSummary, trace_id: str, started_at: datetime) -> Path:
        return Path(thread.workspace_path) / self._relative_trace_path(thread, trace_id, started_at)

    def _relative_trace_path(self, thread: ThreadSummary, trace_id: str, started_at: datetime) -> str:
        return f"traces/{started_at.strftime('%Y%m%d-%H%M')}/{trace_id}.jsonl"

    def _lock_for(self, trace_id: str) -> RLock:
        lock = self._locks.get(trace_id)
        if lock is None:
            raise KeyError(f"Trace lock is not registered: {trace_id}")
        return lock

    def _duration_ms(self, trace_id: str) -> int:
        started = self._started_monotonic.get(trace_id)
        if started is None:
            raise KeyError(f"Trace start time is not registered: {trace_id}")
        return int((time.perf_counter() - started) * 1000)

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)


def _is_deliverable_tool(tool_name: str) -> bool:
    """判断是否为交付物工具（write_file/edit_file，决策 E15/E20）。

    这类工具的 args（含文件 content）需保留进 trace，让评估子代理能从 trace
    读交付物正文 + 中间版本（不依赖 workspace 文件系统存活）。
    其余工具（read_file/ls/grep/task 等）的 args 仍清空控制体积。

    体积实测：write_file 全部事件加起来仅占 trace 0.5%（大头是 llm_start 90%），
    保留正文对总体积影响微乎其微。
    """
    return tool_name in {"write_file", "edit_file"}


def _preserve_deliverable_args(tool_name: str | None, tool_args: Any) -> Any:
    """保留交付物工具的 args（write_file/edit_file 的 content）。

    非 write_file/edit_file 返回 None（保持原 sanitize 行为）。
    write_file/edit_file 返回原 args（含 content + path），让 trace 记录正文。
    """
    if not _is_deliverable_tool(str(tool_name or "")):
        return None
    return tool_args


def _sanitize_event_data(event_data: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event_data.get("type") or "")
    sanitized = dict(event_data)
    if event_type.startswith("llm"):
        pass  # input 保留：包含模型完整输入（系统提示词、注入上下文、对话历史）
    # 交付物工具的 args 保留（write_file/edit_file 的 content，决策 E15/E20），
    # 让 trace 含交付物正文 + 中间版本，评估子代理可从 trace 读正文。
    # 其余工具的 args 仍清空（控制体积，实测 write_file 全部仅占 trace 0.5%）。
    tool_name = str(sanitized.get("tool_name") or "")
    if _is_deliverable_tool(tool_name):
        sanitized["tool_args"] = _preserve_deliverable_args(
            tool_name, sanitized.get("tool_args")
        )
    else:
        sanitized.pop("tool_args", None)
    if "output" in sanitized:
        sanitized["output"] = _sanitize_tool_call_inputs(sanitized["output"])
    if "tool_output" in sanitized:
        sanitized["tool_output"] = _sanitize_tool_call_inputs(sanitized["tool_output"])
    if "tool_calls" in sanitized:
        sanitized["tool_calls"] = _tool_calls_payload(sanitized["tool_calls"])
    return sanitized


def _sanitize_tool_call_inputs(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"tool_calls", "invalid_tool_calls"}:
                sanitized[str(key)] = _tool_calls_payload(item) or []
            else:
                sanitized[str(key)] = _sanitize_tool_call_inputs(item)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_tool_call_inputs(item) for item in value]
    return value


def _tool_calls_payload(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return None
    return [_tool_call_summary(call) for call in value]


def _tool_call_summary(call: Any) -> dict[str, Any]:
    if not isinstance(call, Mapping):
        return {"name": str(call)}
    summary: dict[str, Any] = {}
    for key in ("name", "id", "type"):
        value = call.get(key)
        if value not in (None, ""):
            summary[str(key)] = value
    return summary or {"name": "unknown"}


def _notify_evolution(
    thread: "ThreadSummary",
    trace_id: str,
    status: str,
    run: dict[str, Any],
) -> None:
    """trace 结束后通知监测服务摄入。

    纯副作用、彻底降级：evolution_notify_url 为空 → 不通知；
    任何异常（网络/httpx 未装/超时/连接拒绝）→ 静默吞掉。
    evolution 靠兜底扫描补漏通知的 trace，故单次通知丢失不影响最终一致性。
    """
    try:
        from app.platform.core.settings import get_settings

        url = get_settings().evolution_notify_url
        if not url:
            return  # 未配置 evolution，不通知
        import httpx

        # 桌面化改造（2026-07-07）：内网通知带 X-Notify-Token（evolution NotifyTokenMiddleware 校验）
        notify_token = get_settings().evolution_notify_token
        headers = {"X-Notify-Token": notify_token} if notify_token else None

        httpx.post(
            url,
            json={
                "trace_id": trace_id,
                # Phase 3 重构：不再传 trace_path/workspace_path（evolution 不再读文件，
                # 改调 GET /internal/traces/{trace_id} 拉内容）。
                "status": status,
            },
            headers=headers,
            timeout=_EVOLUTION_NOTIFY_TIMEOUT,
        )
    except Exception:
        # 通知是派生数据，不可拖垮 trace 收尾主流程
        pass
