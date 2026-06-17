"""TraceMiddleware — 代理执行链路追踪中间件。

职责：
  拦截代理的模型调用（LLM）和工具调用（Tool），为每次调用记录
  "开始 / 完成 / 错误" 三种事件，写入 TraceRecorder 供前端展示。

记录的事件字段：
  - type         : llm_start / llm_end / llm_error / tool_start / tool_end / tool_error
  - status       : running / completed / failed
  - source       : 固定为 "middleware"（区分回调来源）
  - agent_name   : 当前代理名称
  - run_id       : LangGraph 运行标识
  - parent_run_id: 父级运行标识（用于构建调用树）
  - duration_ms  : 调用耗时（毫秒）
  - model_name   : 使用的模型名称（仅 LLM 事件）
  - usage        : token 使用量（仅 LLM 完成事件）
  - tool_calls   : 模型请求的工具调用列表（仅 LLM 完成事件）
  - tool_name    : 工具名称（仅 Tool 事件）
  - tool_output  : 工具返回值（仅 Tool 完成事件）
  - error        : 错误信息（仅错误事件）

使用方式：
  在构建代理时加入中间件列表即可，无需额外配置。
  recorder   — TraceRecorder 实例，负责持久化事件
  trace_id   — 当前追踪会话标识
  agent_name — 当前代理名称，用于前端区分不同代理的调用
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from app.writer.trace import TraceRecorder


class TraceMiddleware(AgentMiddleware):
    """代理执行链路追踪中间件。

    通过 DeepAgents 的 AgentMiddleware 接口，以洋葱模型包裹模型调用和工具调用，
    在调用前后插入追踪事件记录。同时提供同步和异步两套接口。
    """

    def __init__(self, recorder: TraceRecorder, trace_id: str, agent_name: str) -> None:
        """
        Args:
            recorder:   追踪记录器，负责将事件持久化到本地存储
            trace_id:   当前追踪会话的唯一标识
            agent_name: 当前代理名称（如 "meta-agent"、"outline" 等）
        """
        self.recorder = recorder
        self.trace_id = trace_id
        self.agent_name = agent_name

    # ------------------------------------------------------------------
    # 模型调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """拦截同步模型调用：记录开始 → 执行 → 记录完成/错误。"""
        started = time.perf_counter()
        self._record_model_start(request)
        try:
            response = handler(request)
        except BaseException as exc:
            # GraphInterrupt 是 HITL（ask_user）的正常控制流，不是错误——
            # 直接放行，不记录 llm_error，避免监测面板误报。
            if not isinstance(exc, GraphInterrupt):
                self._record_model_error(request, started, exc)
            raise
        self._record_model_end(request, response, started)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """拦截异步模型调用：记录开始 → 执行 → 记录完成/错误。"""
        started = time.perf_counter()
        self._record_model_start(request)
        try:
            response = await handler(request)
        except BaseException as exc:
            if not isinstance(exc, GraphInterrupt):
                self._record_model_error(request, started, exc)
            raise
        self._record_model_end(request, response, started)
        return response

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：记录开始 → 执行 → 记录完成/错误。"""
        started = time.perf_counter()
        self._record_tool_start(request)
        try:
            response = handler(request)
        except BaseException as exc:
            # GraphInterrupt 是 HITL（ask_user）的正常控制流，不是工具错误——
            # 直接放行，不记录 tool_error，避免监测面板误报。
            if not isinstance(exc, GraphInterrupt):
                self._record_tool_error(request, started, exc)
            raise
        self._record_tool_end(request, response, started)
        return response

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """拦截异步工具调用：记录开始 → 执行 → 记录完成/错误。"""
        started = time.perf_counter()
        self._record_tool_start(request)
        try:
            response = await handler(request)
        except BaseException as exc:
            if not isinstance(exc, GraphInterrupt):
                self._record_tool_error(request, started, exc)
            raise
        self._record_tool_end(request, response, started)
        return response

    # ------------------------------------------------------------------
    # 内部：模型调用事件记录
    # ------------------------------------------------------------------

    def _record_model_start(self, request: ModelRequest) -> None:
        """记录模型调用开始事件，包含完整输入消息列表和系统提示词。"""
        run_id, parent_run_id = self._current_run_ids()
        messages_payload = _messages_payload(request.messages)
        # 系统提示词在 request.system_message 上，不在 messages 列表中
        system_msg = getattr(request, "system_message", None)
        if system_msg is not None:
            messages_payload = [_message_payload(system_msg)] + (
                messages_payload if isinstance(messages_payload, list) else [messages_payload]
            )
        self.recorder.append_event(
            self.trace_id,
            {
                "type": "llm_start",
                "status": "running",
                "source": "middleware",
                "agent_name": self.agent_name,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "model_name": _model_name(request.model),
                "input": {"messages": messages_payload},
            },
        )

    def _record_model_end(self, request: ModelRequest, response: Any, started: float) -> None:
        """记录模型调用完成事件，包含输出、token 用量和工具调用信息。"""
        run_id, parent_run_id = self._current_run_ids()
        self.recorder.append_event(
            self.trace_id,
            {
                "type": "llm_end",
                "status": "completed",
                "source": "middleware",
                "agent_name": self.agent_name,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "model_name": _model_name(request.model),
                "duration_ms": _duration_ms(started),
                "output": _model_output(response),       # 模型输出（消息列表等）
                "usage": _usage_payload(response),       # token 使用量统计
                "tool_calls": _tool_calls_payload(response),  # 模型请求的工具调用
            },
        )

    def _record_model_error(self, request: ModelRequest, started: float, error: BaseException) -> None:
        """记录模型调用错误事件。"""
        run_id, parent_run_id = self._current_run_ids()
        self.recorder.append_event(
            self.trace_id,
            {
                "type": "llm_error",
                "status": "failed",
                "source": "middleware",
                "agent_name": self.agent_name,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "model_name": _model_name(request.model),
                "duration_ms": _duration_ms(started),
                "error": f"{error.__class__.__name__}: {error}",
            },
        )

    # ------------------------------------------------------------------
    # 内部：工具调用事件记录
    # ------------------------------------------------------------------

    def _record_tool_start(self, request: Any) -> None:
        """记录工具调用开始事件。"""
        tool_call = getattr(request, "tool_call", {})
        run_id, parent_run_id = self._current_run_ids()
        self.recorder.append_event(
            self.trace_id,
            {
                "type": "tool_start",
                "status": "running",
                "source": "middleware",
                "agent_name": self.agent_name,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "tool_call_id": _mapping_value(tool_call, "id"),
                "tool_name": _mapping_value(tool_call, "name"),
            },
        )

    def _record_tool_end(self, request: Any, response: Any, started: float) -> None:
        """记录工具调用完成事件，包含工具输出。"""
        tool_call = getattr(request, "tool_call", {})
        run_id, parent_run_id = self._current_run_ids()
        self.recorder.append_event(
            self.trace_id,
            {
                "type": "tool_end",
                "status": "completed",
                "source": "middleware",
                "agent_name": self.agent_name,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "tool_call_id": _mapping_value(tool_call, "id"),
                "tool_name": _mapping_value(tool_call, "name"),
                "duration_ms": _duration_ms(started),
                "tool_output": _jsonable(response),
            },
        )

    def _record_tool_error(self, request: Any, started: float, error: BaseException) -> None:
        """记录工具调用错误事件。"""
        tool_call = getattr(request, "tool_call", {})
        run_id, parent_run_id = self._current_run_ids()
        self.recorder.append_event(
            self.trace_id,
            {
                "type": "tool_error",
                "status": "failed",
                "source": "middleware",
                "agent_name": self.agent_name,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "tool_call_id": _mapping_value(tool_call, "id"),
                "tool_name": _mapping_value(tool_call, "name"),
                "duration_ms": _duration_ms(started),
                "error": f"{error.__class__.__name__}: {error}",
            },
        )

    # ------------------------------------------------------------------
    # 内部：从 LangGraph 运行时上下文获取 run_id 和父级 run_id
    # ------------------------------------------------------------------

    def _current_run_ids(self) -> tuple[str | None, str | None]:
        """从 LangGraph 配置中提取当前 run_id 及其父级 run_id。

        这两个 ID 用于在前端构建调用树（哪个工具调用属于哪次模型调用）。
        如果获取失败（例如不在 LangGraph 上下文中），返回 (None, None)。
        """
        try:
            config = get_config()
        except RuntimeError:
            return None, None
        metadata = config.get("metadata") or {}
        run_id = metadata.get("run_id") or metadata.get("ls_run_id")
        parent_run_id = self.recorder.run_parent(run_id)
        return str(run_id) if run_id else None, parent_run_id


# ======================================================================
# 模块级工具函数（私有，无状态）
# ======================================================================


def _duration_ms(started: float) -> int:
    """计算从 started 时间点到现在的毫秒数。"""
    return int((time.perf_counter() - started) * 1000)


def _model_name(model: object) -> str:
    """从模型对象中提取模型名称。

    尝试读取 model_name 和 model 属性，如果都没有则返回类名。
    """
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if value:
            return str(value)
    return model.__class__.__name__


def _model_output(response: Any) -> dict[str, object]:
    """从模型响应中提取输出内容。

    提取消息列表和结构化响应（如果有）。
    """
    result = getattr(response, "result", response)
    payload: dict[str, object] = {"messages": _messages_payload(result)}
    structured_response = getattr(response, "structured_response", None)
    if structured_response is not None:
        payload["structured_response"] = _jsonable(structured_response)
    return payload


def _usage_payload(response: Any) -> dict[str, int | None] | None:
    """从模型响应中提取 token 使用量。

    依次尝试从 result、response、model_output 中提取，
    最终归一化为 {input_tokens, output_tokens, total_tokens} 格式。
    """
    result = getattr(response, "result", response)
    usage = _usage_from_value(result)
    if usage is None:
        usage = _usage_from_value(response)
    if usage is None:
        usage = _usage_from_model_output(_model_output(response))
    if usage is None:
        return None
    return _normalize_usage(usage)


def _tool_calls_payload(response: Any) -> object | None:
    """从模型响应中提取工具调用列表的摘要。"""
    result = getattr(response, "result", response)
    calls = _tool_calls_from_value(result)
    return [_tool_call_summary(call) for call in calls] if calls else None


def _messages_payload(value: object) -> object:
    """递归提取消息内容。

    支持单条 BaseMessage、消息列表和字典映射三种格式。
    """
    if isinstance(value, BaseMessage):
        return _message_payload(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_messages_payload(item) for item in value]
    if isinstance(value, Mapping):
        return _mapping_message_payload(value)
    return _jsonable(value)


def _usage_from_value(value: object) -> object | None:
    """从任意值中递归搜索 token 使用量。

    依次检查 usage_metadata、response_metadata.token_usage、response_metadata.usage 等字段，
    支持单条消息、消息列表和字典映射。
    """
    if isinstance(value, BaseMessage):
        response_metadata = getattr(value, "response_metadata", None) or {}
        return (
            getattr(value, "usage_metadata", None)
            or response_metadata.get("token_usage")
            or response_metadata.get("usage")
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        # 从消息列表中取最后一条消息的用量（通常最有意义）
        for item in reversed(value):
            usage = _usage_from_value(item)
            if usage is not None:
                return usage
    response_metadata = getattr(value, "response_metadata", None) or {}
    usage = (
        getattr(value, "usage_metadata", None)
        or response_metadata.get("token_usage")
        or response_metadata.get("usage")
    )
    if usage is not None:
        return usage
    if isinstance(value, Mapping):
        response_metadata = value.get("response_metadata")
        usage = value.get("usage_metadata") or value.get("token_usage") or value.get("usage")
        if usage is None and isinstance(response_metadata, Mapping):
            usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if usage is not None:
            return usage
        return _usage_from_model_output(value)
    return None


def _usage_from_model_output(value: object) -> object | None:
    """从模型输出字典中递归搜索 token 使用量。"""
    if not isinstance(value, Mapping):
        return None
    messages = value.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes | bytearray):
        return None
    for message in reversed(messages):
        usage = _usage_from_value(message)
        if usage is not None:
            return usage
    return None


def _normalize_usage(value: object) -> dict[str, int | None]:
    """将各种格式的 token 使用量归一化为统一结构。

    兼容不同供应商返回的字段名：
    - input_tokens / prompt_tokens  → input_tokens
    - output_tokens / completion_tokens → output_tokens
    - total_tokens（缺失时自动计算）
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    input_tokens = _int_or_none(value.get("input_tokens") or value.get("prompt_tokens"))
    output_tokens = _int_or_none(value.get("output_tokens") or value.get("completion_tokens"))
    total_tokens = _int_or_none(value.get("total_tokens"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def _tool_calls_from_value(value: object) -> list[object]:
    """从值中递归提取工具调用列表。

    支持从 BaseMessage.tool_calls、消息列表和字典映射中提取。
    """
    if isinstance(value, BaseMessage):
        calls = getattr(value, "tool_calls", None)
        return list(calls) if calls else []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        calls: list[object] = []
        for item in value:
            calls.extend(_tool_calls_from_value(item))
        return calls
    if isinstance(value, Mapping):
        calls = value.get("tool_calls")
        return list(calls) if isinstance(calls, Sequence) and not isinstance(calls, str | bytes | bytearray) else []
    return []


def _tool_call_summary(call: object) -> dict[str, object]:
    """将单个工具调用对象转换为摘要字典（只保留 name / id / type）。"""
    if isinstance(call, BaseModel):
        call = call.model_dump(mode="python")
    if not isinstance(call, Mapping):
        return {"name": str(call)}
    summary: dict[str, object] = {}
    for key in ("name", "id", "type"):
        value = call.get(key)
        if value not in (None, ""):
            summary[key] = _jsonable(value)
    return summary or {"name": "unknown"}


def _int_or_none(value: object) -> int | None:
    """安全地将值转换为整数，失败返回 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_payload(message: BaseMessage) -> dict[str, object]:
    """将单条 LangChain 消息转换为可序列化的字典。

    提取字段：type、content、name、id、tool_calls、invalid_tool_calls、
    tool_call_id、usage。
    """
    payload: dict[str, object] = {
        "type": message.type,
        "content": _jsonable(message.content),
    }
    for attr in ("name", "id"):
        value = getattr(message, attr, None)
        if value:
            payload[attr] = str(value)
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = [_tool_call_summary(call) for call in tool_calls]
    invalid_tool_calls = getattr(message, "invalid_tool_calls", None)
    if invalid_tool_calls:
        payload["invalid_tool_calls"] = [_tool_call_summary(call) for call in invalid_tool_calls]
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        payload["tool_call_id"] = _jsonable(tool_call_id)
    response_metadata = getattr(message, "response_metadata", None) or {}
    usage = (
        getattr(message, "usage_metadata", None)
        or response_metadata.get("token_usage")
        or response_metadata.get("usage")
    )
    if usage:
        payload["usage"] = _jsonable(usage)
    return payload


def _mapping_message_payload(message: Mapping[object, object]) -> dict[str, object]:
    """将字典格式的消息转换为可序列化的字典（处理 tool_calls 字段）。"""
    payload = {
        str(key): _jsonable(value)
        for key, value in message.items()
        if key not in {"tool_calls", "invalid_tool_calls"}
    }
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, str | bytes | bytearray):
        payload["tool_calls"] = [_tool_call_summary(call) for call in tool_calls]
    invalid_tool_calls = message.get("invalid_tool_calls")
    if isinstance(invalid_tool_calls, Sequence) and not isinstance(invalid_tool_calls, str | bytes | bytearray):
        payload["invalid_tool_calls"] = [_tool_call_summary(call) for call in invalid_tool_calls]
    return payload


def _mapping_value(value: object, key: str) -> object | None:
    """安全地从映射或对象中取值。"""
    if isinstance(value, Mapping):
        return value.get(key)
    return None


def _jsonable(value: object) -> object:
    """将任意 Python 对象递归转换为 JSON 可序列化结构。

    处理策略（按优先级）：
    1. None / 标量类型（str / int / float / bool） → 直接返回
    2. type 对象 → 转为 {__class__: "type", module, name} 结构
    3. BaseMessage → model_dump 后递归
    4. Pydantic BaseModel → model_dump 后递归
    5. dataclass → dataclasses.asdict 后递归
    6. Mapping → 递归处理每个键值对
    7. Sequence（排除 str/bytes/bytearray）→ 递归处理每个元素
    8. 有 model_dump() 方法的对象 → 调用后递归
    9. 有 dict() 方法的对象 → 调用后递归
    10. 其他 → 转为 {__class__, repr} 结构
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, type):
        return {"__class__": "type", "module": value.__module__, "name": value.__qualname__}
    if isinstance(value, BaseMessage):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        return _jsonable(dumped)
    if hasattr(value, "dict"):
        dumped = value.dict()
        return _jsonable(dumped)
    return {"__class__": value.__class__.__name__, "repr": repr(value)}
