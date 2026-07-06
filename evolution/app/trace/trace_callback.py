"""TraceCallbackHandler — LangChain 回调处理器（从执行端移植，决策 D1）。

注册 run 层级的父子关系（run_id → parent_run_id），配合 TraceMiddleware 构建调用树。

与执行端完全一致，仅 import 路径改为进化端 recorder。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from app.trace.recorder import EvolutionTraceRecorder


class TraceCallbackHandler(BaseCallbackHandler):
    """LangChain 回调处理器，注册 run 层级的父子关系。

    run_inline = True 表示回调在主线程中同步执行，不额外创建线程。
    """

    run_inline = True

    def __init__(self, recorder: EvolutionTraceRecorder, trace_id: str) -> None:
        self.recorder = recorder
        self.trace_id = trace_id

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """当语言模型开始调用时触发，注册为 "llm" 类型运行。"""
        self.recorder.register_run(run_id, parent_run_id, "llm", serialized.get("name"), metadata)

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """当链式调用开始时触发，注册为 "chain" 类型运行。"""
        self.recorder.register_run(run_id, parent_run_id, "chain", (serialized or {}).get("name"), metadata)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """当工具开始调用时触发，注册为 "tool" 类型运行。"""
        self.recorder.register_run(run_id, parent_run_id, "tool", serialized.get("name"), metadata)


__all__ = ["TraceCallbackHandler"]
