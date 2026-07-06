"""进化端 trace 自观测模块（决策 D1：从执行端移植改造）。

为评估 Agent 和进化 Agent 提供与执行端同等的观测能力：
  - EvolutionTraceRecorder：核心记录器（DB 主存 + jsonl WAL）
  - TraceMiddleware：拦截 LLM/Tool 调用产事件
  - TraceCallbackHandler：注册 run 父子关系构建调用树

复用 ingestion 下的 projector/chain_summary（逻辑一致，避免重复）。
schema 走 contracts/trace（单一真源）。
"""
from __future__ import annotations

from app.trace.recorder import EvolutionTraceRecorder, TraceRunHandle
from app.trace.trace_callback import TraceCallbackHandler
from app.trace.trace_middleware import TraceMiddleware

__all__ = [
    "EvolutionTraceRecorder",
    "TraceRunHandle",
    "TraceMiddleware",
    "TraceCallbackHandler",
]
