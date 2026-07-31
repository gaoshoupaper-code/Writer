"""确定性流水线统一观测桥（DEC-001）。

证据编纂和内容评分通过直接 httpx 调 llm.chat，绕过 DeepAgent/TraceMiddleware，
其真实耗时阶段因此不可观测。本模块提供实现 `LlmCallObserver` 的桥，让确定性流水线
把每次模型调用作为 llm span 写进同一 Trace，与 Agent 路径事件同构（CON-001）。

事件契约与 TraceMiddleware 的 llm_start/llm_end/llm_error 完全对齐：
  - source="system"（确定性流水线，区别于 middleware 的 Agent 路径）
  - model_name / duration_ms / phase（业务阶段身份）/ error 字段一致
  - input/output 经截断，敏感正文不因观测扩大暴露（FR-002/FR-003 隐私约束）

非 Agent 调用方显式构造本桥并传给 llm.chat(trace=...)；Agent 路径不使用本桥。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.core.llm import LlmCallObserver
from app.trace.recorder import EvolutionTraceRecorder
from app.trace.trace_middleware import _truncate_payload as _truncate

logger = logging.getLogger("evolution.trace.observers")


class TraceLlmObserver:
    """把确定性 llm.chat 调用桥接成同一条 Trace 的 llm span。

    一个编译/评分流程持有一个实例，绑定固定 trace_id。phase 标识业务阶段
    （如 "contract_parse" / "stage_summary:interview" / "content_score:body"），
    写进 agent_name 让 Trace 详情能按阶段聚合耗时。

    recorder 在 to_thread worker 中调用是安全的：append_event 入内存队列后由
    drain loop 落盘，内部按 trace 加锁（recorder.py _lock_for）。
    """

    def __init__(
        self,
        recorder: EvolutionTraceRecorder,
        trace_id: str,
        *,
        component: str = "dossier-compiler",
    ) -> None:
        self._recorder = recorder
        self._trace_id = trace_id
        # component 写进 agent_name，区分来源（dossier-compiler / content-scoring）。
        self._component = component

    def _emit(self, values: dict[str, Any]) -> None:
        """安全写入 trace 事件：recorder 异常（trace 已 finalize / 状态机异常）绝不向上传播。

        观测桥的契约是「记录而不扩大故障面」（CON-001）。若 recorder 抛 KeyError
        （trace 已 complete/cleanup）或其它异常，只 log 降级——绝不能让观测设施的故障
        把一次本应成功的 llm.chat 判为失败（那会让观测本身成为评分/编译失败源）。
        """
        try:
            self._recorder.append_event(self._trace_id, values)
        except Exception:
            logger.warning("trace 观测写入降级（recorder 状态异常），不影响业务调用", exc_info=True)

    def _emit_business(self, tool: str, status: str, **extra: Any) -> None:
        """安全写入业务阶段事件（同 _emit 的容错语义）。"""
        try:
            self._recorder.append_business_event(self._trace_id, tool, status, **extra)
        except Exception:
            logger.warning("trace 业务事件写入降级（recorder 状态异常）", exc_info=True)

    def on_llm_start(self, *, phase: str, model: str, messages: list[dict[str, str]]) -> None:
        self._emit(
            {
                "type": "llm_start",
                "status": "running",
                "source": "system",
                "agent_name": self._component,
                "model_name": model,
                "node_name": phase,
                # 截断：输入含完整 system prompt + 待评正文，必须控体积且不扩大暴露。
                "input": _truncate({"phase": phase, "messages": messages}),
            },
        )

    def on_llm_end(
        self, *, phase: str, model: str, duration_ms: float, output: str,
    ) -> None:
        # output schema 必须与 TraceMiddleware 的 {messages:[...]} 契约对齐：
        # 前端抽屉/投影/usage 提取都依赖 output.messages（EVD-004 / FR-005）。
        # 把确定性 llm.chat 的纯文本回复包成单条 ai 消息，phase 作为同级元数据保留。
        self._emit(
            {
                "type": "llm_end",
                "status": "completed",
                "source": "system",
                "agent_name": self._component,
                "model_name": model,
                "node_name": phase,
                "duration_ms": duration_ms,
                "output": _truncate({
                    "phase": phase,
                    "messages": [{"type": "ai", "content": output}],
                }),
            },
        )

    def on_llm_error(
        self, *, phase: str, model: str, duration_ms: float, error: BaseException,
    ) -> None:
        self._emit(
            {
                "type": "llm_error",
                "status": "failed",
                "source": "system",
                "agent_name": self._component,
                "model_name": model,
                "node_name": phase,
                "duration_ms": duration_ms,
                "error": f"{error.__class__.__name__}: {error}",
            },
        )

    # ── 业务阶段 span（确定性阶段的开始/结束，非模型调用）──

    def phase_start(self, phase: str, **extra: Any) -> None:
        """记录一个确定性阶段的开始（事实提取/契约解析/落库等）。"""
        self._emit_business(
            "phase", "running",
            phase=phase, message=extra.pop("message", None) or phase, **extra,
        )

    def phase_end(self, phase: str, *, duration_ms: float | None = None, **extra: Any) -> None:
        """记录一个确定性阶段的结束。"""
        payload: dict[str, Any] = {"phase": phase}
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        payload.update(extra)
        self._emit_business("phase", "completed", **payload)

    def phase_fail(self, phase: str, *, error: str, duration_ms: float | None = None) -> None:
        """记录一个确定性阶段的失败。"""
        payload: dict[str, Any] = {"phase": phase, "error": error}
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self._emit_business("phase", "failed", **payload)


def elapsed_ms(started: float) -> float:
    """配套工具：从 perf_counter 起点算毫秒。"""
    return round((time.perf_counter() - started) * 1000.0, 1)


# 确认本类满足 llm.chat 的观察者协议（静态自检，防接口漂移）。
_: LlmCallObserver = TraceLlmObserver  # type: ignore[assignment]


__all__ = ["TraceLlmObserver", "elapsed_ms"]
