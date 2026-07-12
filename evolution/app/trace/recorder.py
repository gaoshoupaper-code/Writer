"""进化端 trace 记录器（决策 D1：从执行端 recorder 移植改造）。

与执行端 recorder 的核心差异（设计决策 D7-D9）：
  - 存储后端：DB 主存储（event_payloads/runs/nodes）+ jsonl WAL（崩溃恢复兜底）
  - 身份模型：session_id 替代 ThreadSummary，无 workspace/thread 概念
  - 投影策略：append_event 实时写 event_payloads；终态批量投影 nodes（D7）
  - runs 入库：create_run 时即写（status=running），终态 UPDATE（D8）
  - WAL 顺序：drain 先写 DB 后写 jsonl WAL（D9）
  - 砍掉：HITL（resume/await/stop）、zombie scanner、查询类、通知 evolution

新增能力：
  - append_business_event：替代 emit_step，业务事件注入同一 trace 流（D3）
  - recover_pending：进程重启后扫描 running trace 补终态（D8 崩溃恢复 R1）

数据流：
  middleware 拦截 LLM/Tool → append_event → 内存 deque + asyncio.Queue
    → drain 协程批量：executemany DB（event_payloads）→ append jsonl WAL
    → asyncio.Queue 供 SSE trace_pump 消费
  trace 终态（complete/fail/cancel）→ projector 批量投影 → executemany nodes
"""
from __future__ import annotations

import asyncio
import json
import logging
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

import app.core.db as db
from app.core.models import (
    TraceContextRange,
    TraceDetail,
    TraceLogEvent,
    TraceNode,
    TraceRunSummary,
)
from app.trace.increment import IncrementState, compute_increment
from app.trace.summary_export import export_trace_summary

logger = logging.getLogger("evolution.trace.recorder")

# ── 写盘解耦参数（与执行端一致）──
_DRAIN_INTERVAL = 0.5  # 后台 drain 协程每 0.5s 成批写一次
_FLUSH_BATCH_MAX = 200  # 单批最多写多少行

# WAL 落盘根目录（evolution/data/traces/）。
_WAL_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "traces"

# trace 终态的取消来源（与执行端对齐）。
CancelReason = Literal["client_disconnect", "timeout", "crash_recovery", "user_stop"]
_CANCEL_REASON_MESSAGES: dict[str, str] = {
    "client_disconnect": "Session cancelled",
    "timeout": "Session timeout",
    "crash_recovery": "Recovered from crash (process restart)",
    "user_stop": "Stopped by user",
}


@dataclass
class TraceRunHandle:
    """一次 trace run 的句柄，持有供 SSE 消费的事件队列。"""

    trace_id: str
    queue: asyncio.Queue[TraceLogEvent] = field(default_factory=asyncio.Queue)


class EvolutionTraceRecorder:
    """进化端 trace 记录器。

    单例（main.py lifespan 创建）。负责评估 Agent 和进化 Agent 的自观测 trace。
    """

    def __init__(self) -> None:
        self._locks: dict[str, RLock] = {}
        self._sequences: dict[str, int] = {}
        self._queues: dict[str, asyncio.Queue[TraceLogEvent]] = {}
        self._started_monotonic: dict[str, float] = {}
        self._wal_paths: dict[str, Path] = {}
        self._run_purposes: dict[str, str] = {}
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._increment_states: dict[str, IncrementState] = {}
        # session_id → trace_id 映射（供 SSE 端点按 session_id 查 trace_id_self）。
        self._session_trace: dict[str, str] = {}
        self._anchor_counter: int = 0
        # SSE 运行期投影 diff（Phase 2 T4 路线 Y）：per trace 的 differ。
        self._differs: dict[str, Any] = {}
        # 写盘解耦缓冲（与执行端一致）。
        self._pending_writes: deque[tuple[str, str]] = deque()  # (trace_id, json_line)
        self._pending_lock = RLock()
        self._drain_task: asyncio.Task[None] | None = None

    # ── 生命周期 ──────────────────────────────────────────────

    def start_drain(self) -> None:
        """启动后台 drain 协程（lifespan 调用，幂等）。"""
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop())

    async def aclose(self) -> None:
        """关闭：停 drain + flush 残余事件落盘。"""
        if self._drain_task is not None:
            self._drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None
        self._flush_all_sync()

    # ── run 生命周期 ──────────────────────────────────────────

    def create_run(
        self,
        session_id: str,
        run_purpose: str,
        *,
        endpoint: str = "",
    ) -> TraceRunHandle:
        """创建一条 trace run（D8：立即写 runs 行 status=running）。

        Args:
            session_id: 评估/进化 session id（作为 session_name）
            run_purpose: evolution_eval / evolution_evolve
            endpoint: 可选，记录触发端点
        """
        trace_id = f"trace-{uuid4().hex}"
        started_at = datetime.now(UTC)
        started_iso = started_at.isoformat()
        wal_path = self._wal_path(trace_id, started_at)
        wal_path.parent.mkdir(parents=True, exist_ok=True)

        self._locks[trace_id] = RLock()
        self._sequences[trace_id] = 0
        self._queues[trace_id] = asyncio.Queue()
        self._started_monotonic[trace_id] = time.perf_counter()
        self._wal_paths[trace_id] = wal_path
        self._run_purposes[trace_id] = run_purpose
        self._increment_states[trace_id] = IncrementState()
        # 登记 session_id → trace_id 映射（供 SSE 端点查询）。
        self._session_trace[session_id] = trace_id
        # 初始化 SSE diff 引擎（Phase 2 T4 路线 Y）。
        from app.trace.differ import NodeSnapshotDiffer
        self._differs[trace_id] = NodeSnapshotDiffer()

        # D8：create_run 即写 runs 行（status=running），运行中前端可见。
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint,
                status, started_at, event_count, ingested_at, run_purpose)
               VALUES (?, ?, ?, ?, ?, 'running', ?, 0, ?, ?)""",
            (
                trace_id,
                "evolution",       # workspace_id：进化端固定标识
                session_id,        # thread_id：复用列存 session_id
                session_id,        # session_name
                endpoint or run_purpose,
                started_iso,
                started_iso,       # ingested_at
                run_purpose,
            ),
        )

        handle = TraceRunHandle(trace_id=trace_id, queue=self._queues[trace_id])

        # 写 run_start 事件。
        self.append_event(
            trace_id,
            {
                "type": "run_start",
                "status": "running",
                "source": "system",
                "input": {
                    "endpoint": endpoint or run_purpose,
                    "session_id": session_id,
                    "run_purpose": run_purpose,
                },
            },
        )
        return handle

    def complete_run(self, trace_id: str) -> TraceLogEvent:
        """正常完成：写 run_end + 终态投影 + UPDATE runs。"""
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
        self._finalize_run(trace_id, "completed", duration_ms, None)
        return event

    def fail_run(self, trace_id: str, error: BaseException | str) -> TraceLogEvent:
        """失败收尾：写 run_error + 终态投影 + UPDATE runs。"""
        error_msg = (
            f"{error.__class__.__name__}: {error}"
            if isinstance(error, BaseException)
            else str(error)
        )
        duration_ms = self._duration_ms(trace_id)
        event = self.append_event(
            trace_id,
            {
                "type": "run_error",
                "status": "failed",
                "source": "system",
                "duration_ms": duration_ms,
                "error": error_msg,
            },
        )
        self._finalize_run(trace_id, "failed", duration_ms, error_msg)
        return event

    def cancel_run(
        self, trace_id: str, reason: CancelReason = "client_disconnect"
    ) -> TraceLogEvent:
        """取消收尾（终态 cancelled）。"""
        error_message = _CANCEL_REASON_MESSAGES.get(reason, "Cancelled")
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
        self._finalize_run(trace_id, "cancelled", duration_ms, error_message)
        return event

    # ── run 父子关系注册（TraceCallbackHandler 用）──

    def register_run(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        kind: str,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """注册 run 层级父子关系（内存，不持久化）。"""
        key = str(run_id)
        self._run_metadata.setdefault("__run_parents__", {})[key] = (
            str(parent_run_id) if parent_run_id else None
        )
        self._run_metadata.setdefault("__run_kinds__", {})[key] = kind
        if name:
            self._run_metadata.setdefault("__run_names__", {})[key] = name

    def run_parent(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._run_metadata.get("__run_parents__", {}).get(str(run_id))

    # ── 事件写入（核心）──

    def append_event(self, trace_id: str, values: dict[str, Any]) -> TraceLogEvent:
        """追加一条事件（middleware 框架事件 + business_step 业务事件共用）。

        流程：分配 seq/anchor → 增量计算 → 构造 TraceLogEvent
              → 入内存 deque（drain 批量写 DB+WAL）→ 入 asyncio.Queue（供 SSE）
        """
        lock = self._lock_for(trace_id)
        with lock:
            sequence = self._sequences.get(trace_id)
            if sequence is None:
                raise KeyError(f"Trace run is not active: {trace_id}")
            sequence += 1
            self._sequences[trace_id] = sequence

            event_type = str(values["type"])

            # 分配稳定 anchor_id。
            self._anchor_counter += 1
            output_anchor_id = f"anchor-{trace_id}-{sequence}-{self._anchor_counter}"

            # 增量存储：仅 llm_start 且有 input 时计算。
            input_value = values.get("input")
            input_context_range = values.get("input_context_range")
            if event_type == "llm_start" and input_value is not None:
                inc_state = self._increment_states.get(trace_id)
                if inc_state is not None:
                    result = compute_increment(inc_state, input_value, output_anchor_id)
                    input_value = result.input_to_store
                    input_context_range = result.input_context_range

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

            # 入内存缓冲（drain 批量落盘）。
            json_line = event.model_dump_json(exclude_none=True)
            with self._pending_lock:
                self._pending_writes.append((trace_id, json_line))

            # 入 asyncio.Queue（供 SSE 消费，与执行端一致）。
            queue = self._queues.get(trace_id)
            if queue is not None:
                queue.put_nowait(event)

            return event

    def append_business_event(
        self,
        self_trace_id: str,
        tool: str,
        status: str,
        *,
        phase: str | None = None,
        message: str | None = None,
        **extra: Any,
    ) -> TraceLogEvent:
        """注入业务事件（D3：替代 emit_step/emit_log）。

        业务事件作为 trace 流的一部分，和 middleware 框架事件统一进队列。
        phase/status/message 等业务语义塞进 input metadata，SSE 端从 trace 事件派生。

        Args:
            self_trace_id: 自观测 trace id（本次评估/进化的录像）
            tool:          工具/步骤名（如 read_trace / apply_edits）
            status:        running / done / failed / blocked
            phase:         流程阶段（plan / execute / eval）
            message:       思考日志文本（emit_log 的等价物）
            **extra:       额外业务字段（如 input_trace_id / error / file_path）
        """
        input_payload: dict[str, Any] = {"tool": tool, "status": status}
        if phase is not None:
            input_payload["phase"] = phase
        if message is not None:
            input_payload["message"] = message
        input_payload.update(extra)

        return self.append_event(
            self_trace_id,
            {
                "type": "run_meta",  # 复用 run_meta 事件类型（执行端也用它记元信息）
                "status": status if status in ("running", "completed", "failed", "cancelled") else "running",
                "source": "system",
                "input": input_payload,
            },
        )

    # ── 终态收尾（投影 + UPDATE runs + 清理）──

    def _finalize_run(
        self,
        trace_id: str,
        status: str,
        duration_ms: int,
        error: str | None,
    ) -> None:
        """trace 终态收尾：flush 残余 → 投影 nodes → UPDATE runs → 清理内存。"""
        # 先 flush 该 trace 残余事件（保证 event_payloads + WAL 完整）。
        self.flush_sync(trace_id)

        # 记录 prompt 版本（若有）。
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
            self.flush_sync(trace_id)

        # D7：终态批量投影 → 写 nodes 表。
        self._project_and_write_nodes(trace_id, status)

        # UPDATE runs 终态。
        seq = self._sequences.get(trace_id, 0)
        db.execute(
            """UPDATE runs
               SET status=?, ended_at=?, duration_ms=?, event_count=?, error=?
               WHERE trace_id=?""",
            (status, self._now(), duration_ms, seq, error, trace_id),
        )

        # 导出 summary JSON。
        self._export_summary(trace_id, status)

        # 清理内存活跃态。
        self._cleanup_run_state(trace_id)

    def _project_and_write_nodes(self, trace_id: str, status: str) -> None:
        """终态投影：读 event_payloads → projector.project → executemany nodes。"""
        from app.ingestion.projector import TraceProjector

        try:
            events = self._load_events_from_db(trace_id)
            if not events:
                return
            run_row = db.query_one(
                "SELECT * FROM runs WHERE trace_id=?", (trace_id,)
            )
            if run_row is None:
                return
            run = TraceRunSummary(
                trace_id=trace_id,
                workspace_id=run_row["workspace_id"],
                thread_id=run_row["thread_id"] or "",
                session_name=run_row["session_name"] or "",
                workspace_path="",
                endpoint=run_row["endpoint"] or "",
                status=status,  # type: ignore[arg-type]
                started_at=run_row["started_at"] or "",
                ended_at=run_row.get("ended_at"),
                duration_ms=run_row["duration_ms"],
                event_count=run_row["event_count"] or 0,
                path="",
                error=run_row.get("error"),
            )
            projection = TraceProjector().project(run, events)

            # 批量写 nodes（先删旧再插，幂等）。
            db.execute("DELETE FROM nodes WHERE trace_id=?", (trace_id,))
            if projection.nodes:
                db.executemany(
                    """INSERT INTO nodes
                       (node_id, trace_id, parent_node_id, kind, label, status,
                        agent_name, agent_role, depth, started_at, ended_at,
                        duration_ms, model_name, tool_name, skill_name,
                        usage_input, usage_output, usage_total, chain_summary, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            n.node_id, trace_id, n.parent_node_id, n.kind, n.label,
                            n.status, n.agent_name, n.agent_role, n.depth,
                            n.started_at, n.ended_at, n.duration_ms, n.model_name,
                            n.tool_name, n.skill_name,
                            n.usage.input_tokens if n.usage else None,
                            n.usage.output_tokens if n.usage else None,
                            n.usage.total_tokens if n.usage else None,
                            n.chain_summary, n.error,
                        )
                        for n in projection.nodes
                    ],
                )
        except Exception:
            logger.exception("投影 nodes 失败 trace=%s（不影响 trace 终态）", trace_id)

    def _export_summary(self, trace_id: str, status: str) -> None:
        """导出模型可读 summary JSON。"""
        try:
            events = self._load_events_from_db(trace_id)
            run_row = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
            if run_row is None or not events:
                return
            from app.ingestion.projector import TraceProjector

            run = TraceRunSummary(
                trace_id=trace_id,
                workspace_id=run_row["workspace_id"],
                thread_id=run_row["thread_id"] or "",
                session_name=run_row["session_name"] or "",
                workspace_path="",
                endpoint=run_row["endpoint"] or "",
                status=status,  # type: ignore[arg-type]
                started_at=run_row["started_at"] or "",
                ended_at=run_row.get("ended_at"),
                duration_ms=run_row["duration_ms"],
                event_count=run_row["event_count"] or 0,
                path="",
                error=run_row.get("error"),
            )
            projection = TraceProjector().project(run, events)
            detail = TraceDetail(
                run=run, events=events, nodes=projection.nodes,
                context=projection.context, todos=projection.todos,
            )
            wal_path = self._wal_paths.get(trace_id)
            if wal_path:
                summary_path = wal_path.with_name(f"{trace_id}_summary.json")
                export_trace_summary(detail, summary_path)
        except Exception:
            logger.exception("导出 summary 失败 trace=%s", trace_id)

    def _load_events_from_db(self, trace_id: str) -> list[TraceLogEvent]:
        """从 event_payloads 表读事件列表。"""
        rows = db.query_all(
            "SELECT payload_json FROM event_payloads WHERE trace_id=? ORDER BY sequence",
            (trace_id,),
        )
        return [TraceLogEvent.model_validate(json.loads(r["payload_json"])) for r in rows]

    # ── 崩溃恢复（D8 R1）──

    def recover_pending(self) -> int:
        """进程重启后扫描 status=running 的 runs，补终态（D8 崩溃恢复 R1）。

        扫描 runs 表所有 status=running 的行 → 每条标 failed（crash_recovery）。
        event_payloads 已实时写入（数据不丢），nodes 在 _finalize_run 里投影。

        Returns: 恢复的 trace 数量。
        """
        rows = db.query_all("SELECT trace_id FROM runs WHERE status='running'")
        count = 0
        for row in rows:
            trace_id = row["trace_id"]
            logger.warning("崩溃恢复：trace %s 标记为 failed", trace_id)
            # 重建最小活跃态让 _finalize_run 能 append run_error 事件。
            self._rebuild_minimal_state(trace_id)
            try:
                self._finalize_run(trace_id, "failed", 0, _CANCEL_REASON_MESSAGES["crash_recovery"])
                count += 1
            except Exception:
                logger.exception("崩溃恢复失败 trace=%s", trace_id)
                # 兜底：直接 UPDATE runs。
                db.execute(
                    "UPDATE runs SET status='failed', error=? WHERE trace_id=?",
                    (_CANCEL_REASON_MESSAGES["crash_recovery"], trace_id),
                )
            finally:
                self._cleanup_run_state(trace_id)
        return count

    def _rebuild_minimal_state(self, trace_id: str) -> None:
        """重建最小活跃态让 append_event 能工作（崩溃恢复用）。"""
        row = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
        if row is None:
            return
        started_at = row["started_at"] or datetime.now(UTC).isoformat()
        try:
            dt = datetime.fromisoformat(started_at)
        except (ValueError, TypeError):
            dt = datetime.now(UTC)
        wal_path = self._wal_path(trace_id, dt)
        self._locks[trace_id] = RLock()
        self._sequences[trace_id] = row["event_count"] or 0
        self._queues[trace_id] = asyncio.Queue()
        self._started_monotonic[trace_id] = time.perf_counter()
        self._wal_paths[trace_id] = wal_path
        self._run_purposes[trace_id] = row.get("run_purpose") or "unknown"
        # _increment_states 不重建 → 退化为全量（安全降级）。

    # ── 写盘解耦（drain + flush）──

    def _drain_active(self) -> bool:
        return self._drain_task is not None and not self._drain_task.done()

    async def _drain_loop(self) -> None:
        """后台循环：周期性成批写 DB + WAL。"""
        while True:
            await asyncio.sleep(_DRAIN_INTERVAL)
            batch = self._take_pending_batch()
            if batch:
                await asyncio.to_thread(self._write_batch_sync, batch)

    def _take_pending_batch(self) -> list[tuple[str, str]]:
        """从缓冲区取出一批待写行（最多 _FLUSH_BATCH_MAX 条）。"""
        with self._pending_lock:
            count = min(len(self._pending_writes), _FLUSH_BATCH_MAX)
            if count == 0:
                return []
            batch = [self._pending_writes.popleft() for _ in range(count)]
        return batch

    def _write_batch_sync(self, batch: list[tuple[str, str]]) -> None:
        """同步写一批事件（D9：先 DB 后 WAL）。

        此方法在 to_thread 的线程池 worker 里执行，不占事件循环。
        """
        # 1. 先写 DB（event_payloads）—— 主存储。
        db_rows = []
        for trace_id, json_line in batch:
            try:
                data = json.loads(json_line)
                db_rows.append((
                    trace_id,
                    data.get("sequence"),
                    data.get("type"),
                    data.get("timestamp"),
                    json_line,
                ))
            except (json.JSONDecodeError, KeyError):
                continue
        if db_rows:
            try:
                db.executemany(
                    """INSERT INTO event_payloads (trace_id, sequence, type, timestamp, payload_json)
                       VALUES (?,?,?,?,?)""",
                    db_rows,
                )
            except Exception:
                logger.exception("drain 写 DB event_payloads 失败（%d 条）", len(db_rows))

        # 2. 后写 WAL jsonl —— 按 trace 分组 append。
        grouped: dict[str, list[str]] = {}
        for trace_id, json_line in batch:
            grouped.setdefault(trace_id, []).append(json_line)
        for trace_id, lines in grouped.items():
            wal_path = self._wal_paths.get(trace_id)
            if wal_path is None:
                continue
            try:
                with wal_path.open("a", encoding="utf-8") as f:
                    f.writelines(f"{line}\n" for line in lines)
            except OSError:
                pass

    def flush_sync(self, trace_id: str) -> None:
        """同步刷掉指定 trace 的残余事件（终态收尾调用）。"""
        pending: list[str] = []
        with self._pending_lock:
            rest: list[tuple[str, str]] = []
            while self._pending_writes:
                tid, line = self._pending_writes.popleft()
                if tid == trace_id:
                    pending.append(line)
                else:
                    rest.append((tid, line))
            self._pending_writes.extendleft(reversed(rest))
        if pending:
            self._write_batch_sync([(trace_id, line) for line in pending])

    def _flush_all_sync(self) -> None:
        """同步刷掉所有残余事件（aclose 关闭时调用）。"""
        with self._pending_lock:
            batch = list(self._pending_writes)
            self._pending_writes.clear()
        if batch:
            self._write_batch_sync(batch)

    # ── 查询（供 SSE / 活跃大盘）──

    def get_active_queue(self, trace_id: str) -> asyncio.Queue[TraceLogEvent] | None:
        """取 trace 的事件队列（SSE trace_pump 消费用）。"""
        return self._queues.get(trace_id)

    def get_trace_id_by_session(self, session_id: str) -> str | None:
        """按 session_id 查自观测 trace_id（SSE 端点用）。

        返回 None 表示 session 不存在或 trace 已终态（队列已清）。
        """
        return self._session_trace.get(session_id)

    def is_terminal(self, trace_id: str) -> bool:
        """trace 是否已终态（SSE 端点判断流是否结束）。"""
        return trace_id not in self._queues

    def list_active_runs(self) -> list[dict[str, Any]]:
        """列出当前活跃 trace（监测面板活跃大盘，纯内存读取）。"""
        now = time.perf_counter()
        result: list[dict[str, Any]] = []
        for trace_id, started in self._started_monotonic.items():
            result.append({
                "trace_id": trace_id,
                "endpoint": self._run_purposes.get(trace_id, ""),
                "duration_ms": int((now - started) * 1000),
                "event_count": self._sequences.get(trace_id, 0),
                "status": "running",
                "run_purpose": self._run_purposes.get(trace_id, ""),
            })
        return result

    # ── 运行期投影 diff（Phase 2 T4 路线 Y，供 SSE 推 node patch）──

    def project_and_diff(self, trace_id: str) -> dict[str, list[TraceNode]] | None:
        """从 DB 全量投影 → 与前次快照 diff → 返回增量 patch。

        SSE 端点周期性调用此方法，把 patch 推给前端。
        返回 None 表示 trace 不活跃或无事件。

        路线 Y：projector 保持全量无状态，每次全量投影后 diff 出变更的 node。
        单次 O(N)，节流后（500ms）扛得住上千事件。
        """
        from app.ingestion.projector import TraceProjector
        from app.ingestion.increment import reconstruct_all_inputs

        differ = self._differs.get(trace_id)
        if differ is None:
            return None

        # 先 flush 该 trace 残余事件（保证 DB 有最新数据）。
        self.flush_sync(trace_id)

        events_raw = self._load_raw_events(trace_id)
        if not events_raw:
            return None

        # 增量重建（单次 O(N)）
        reconstructed = reconstruct_all_inputs(events_raw)
        if reconstructed:
            for e_raw in events_raw:
                full_input = reconstructed.get(e_raw.get("event_id"))
                if full_input is not None:
                    e_raw["input"] = full_input
        events = [TraceLogEvent.model_validate(e) for e in events_raw]

        run_row = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
        if run_row is None:
            return None
        run = TraceRunSummary(
            trace_id=trace_id,
            workspace_id=run_row["workspace_id"],
            thread_id=run_row["thread_id"] or "",
            session_name=run_row["session_name"] or "",
            workspace_path="",
            endpoint=run_row["endpoint"] or "",
            status=run_row["status"],  # type: ignore[arg-type]
            started_at=run_row["started_at"] or "",
            ended_at=run_row.get("ended_at"),
            duration_ms=run_row["duration_ms"],
            event_count=run_row["event_count"] or 0,
            path="",
            error=run_row.get("error"),
        )

        projection = TraceProjector().project(run, events)
        return differ.diff(projection.nodes)

    def project_full_nodes(self, trace_id: str) -> list[TraceNode] | None:
        """全量投影 → 返回完整 nodes 列表（终态 snapshot 用）。

        SSE 终态时调用，推全量 snapshot 强制前端对齐（T9）。
        """
        from app.ingestion.projector import TraceProjector
        from app.ingestion.increment import reconstruct_all_inputs

        events_raw = self._load_raw_events(trace_id)
        if not events_raw:
            return None

        reconstructed = reconstruct_all_inputs(events_raw)
        if reconstructed:
            for e_raw in events_raw:
                full_input = reconstructed.get(e_raw.get("event_id"))
                if full_input is not None:
                    e_raw["input"] = full_input
        events = [TraceLogEvent.model_validate(e) for e in events_raw]

        run_row = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
        if run_row is None:
            return None
        run = TraceRunSummary(
            trace_id=trace_id,
            workspace_id=run_row["workspace_id"],
            thread_id=run_row["thread_id"] or "",
            session_name=run_row["session_name"] or "",
            workspace_path="",
            endpoint=run_row["endpoint"] or "",
            status=run_row["status"],  # type: ignore[arg-type]
            started_at=run_row["started_at"] or "",
            ended_at=run_row.get("ended_at"),
            duration_ms=run_row["duration_ms"],
            event_count=run_row["event_count"] or 0,
            path="",
            error=run_row.get("error"),
        )
        projection = TraceProjector().project(run, events)
        return projection.nodes

    def _load_raw_events(self, trace_id: str) -> list[dict[str, Any]]:
        """从 DB 加载事件（dict 形态，未反序列化为 TraceLogEvent）。"""
        rows = db.query_all(
            "SELECT payload_json FROM event_payloads WHERE trace_id=? ORDER BY sequence",
            (trace_id,),
        )
        return [json.loads(r["payload_json"]) for r in rows]

    # ── prompt 版本（与执行端一致）──

    def set_prompt_version(self, trace_id: str, prompt_name: str, version: int) -> None:
        """记录本次 trace 使用的 prompt 版本。"""
        meta = self._run_metadata.setdefault(trace_id, {})
        meta.setdefault("prompt_versions", {})[prompt_name] = version

    # ── 内部工具 ──────────────────────────────────────────────

    def _cleanup_run_state(self, trace_id: str) -> None:
        self._queues.pop(trace_id, None)
        self._started_monotonic.pop(trace_id, None)
        self._wal_paths.pop(trace_id, None)
        self._locks.pop(trace_id, None)
        self._sequences.pop(trace_id, None)
        self._increment_states.pop(trace_id, None)
        self._run_purposes.pop(trace_id, None)
        self._differs.pop(trace_id, None)

    def _lock_for(self, trace_id: str) -> RLock:
        lock = self._locks.get(trace_id)
        if lock is None:
            raise KeyError(f"Trace lock is not registered: {trace_id}")
        return lock

    def _duration_ms(self, trace_id: str) -> int:
        started = self._started_monotonic.get(trace_id)
        if started is None:
            return 0
        return int((time.perf_counter() - started) * 1000)

    def _wal_path(self, trace_id: str, started_at: datetime) -> Path:
        return _WAL_ROOT / started_at.strftime("%Y%m%d-%H%M") / f"{trace_id}.jsonl"

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)


# ── 体积控制工具函数（从执行端移植，逻辑一致）──


def _is_deliverable_tool(tool_name: str) -> bool:
    """判断是否为交付物工具（write_file/edit_file，决策 E15/E20）。"""
    return tool_name in {"write_file", "edit_file"}


def _preserve_deliverable_args(tool_name: str | None, tool_args: Any) -> Any:
    """保留交付物工具的 args，其余返回 None（控体积）。"""
    if not _is_deliverable_tool(str(tool_name or "")):
        return None
    return tool_args


def _sanitize_tool_call_inputs(value: Any) -> Any:
    """递归清理 tool_calls/invalid_tool_calls 为摘要（控体积）。"""
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


__all__ = ["EvolutionTraceRecorder", "TraceRunHandle"]
