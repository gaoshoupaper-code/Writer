"""TraceCallbackHandler — LangChain 回调处理器，用于注册 run 层级的父子关系。

职责：
  与 TraceMiddleware 配合使用。TraceMiddleware 在中间件层拦截模型/工具调用，
  而 TraceCallbackHandler 在 LangChain 的回调层注册每次运行（run）的层级关系。

  三种运行类型：
  - llm:   语言模型调用
  - chain: 链式调用（包含代理本身的运行）
  - tool:  工具调用

  注册的父子关系（run_id → parent_run_id）用于 TraceMiddleware 在记录事件时
  构建完整的调用树，供前端 TracePanel 展示。

使用方式：
  将此处理器传入 agent.invoke() 的 config.callbacks 参数中。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from app.writer.trace import TraceRecorder


class TraceCallbackHandler(BaseCallbackHandler):
    """LangChain 回调处理器，注册 run 层级的父子关系。

    run_inline = True 表示回调在主线程中同步执行，不额外创建线程。
    """

    run_inline = True

    def __init__(self, recorder: TraceRecorder, trace_id: str) -> None:
        """
        Args:
            recorder:  追踪记录器，用于注册 run 的父子关系
            trace_id:  当前追踪会话的唯一标识
        """
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
