from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import openai
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

_REASONING_CONTENT_KEY = "reasoning_content"
_TOOL_CALLS_KEY = "tool_calls"


class ReasoningSidecarStore:
    def __init__(self) -> None:
        self._records_by_tool_call_id: dict[str, str] = {}

    def save(self, message: AIMessage) -> None:
        reasoning_content = extract_reasoning_content(message)
        if not reasoning_content:
            return

        tool_call_ids = tool_call_ids_from_message(message)
        if not tool_call_ids:
            return

        for tool_call_id in tool_call_ids:
            self._records_by_tool_call_id[tool_call_id] = reasoning_content

    def reasoning_for_message(self, message: AIMessage) -> str | None:
        for tool_call_id in tool_call_ids_from_message(message):
            reasoning_content = self._records_by_tool_call_id.get(tool_call_id)
            if reasoning_content:
                return reasoning_content
        return None


class DeepSeekThinkingChatModel(ChatOpenAI):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._reasoning_store = ReasoningSidecarStore()

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        source_messages = self._convert_input(input_).to_messages()
        for message in source_messages:
            if isinstance(message, AIMessage):
                self._reasoning_store.save(message)

        messages = payload.get("messages")
        if messages is None:
            raise RuntimeError("DeepSeek thinking mode requires chat/completions message payloads.")
        if not isinstance(messages, list):
            raise TypeError("DeepSeek thinking message payload must be a list.")
        payload["messages"] = [self._hydrate_payload_message(message) for message in messages]
        return payload

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = _response_to_dict(response)
        choices = response_dict.get("choices")
        if choices is None:
            raise KeyError("DeepSeek response missing choices.")
        if not isinstance(choices, Sequence) or isinstance(choices, str | bytes | bytearray):
            raise TypeError("DeepSeek response choices must be a sequence.")

        for generation, choice in zip(result.generations, choices, strict=False):
            if not isinstance(choice, Mapping):
                raise TypeError("DeepSeek response choice must be a mapping.")
            raw_message = choice.get("message")
            if not isinstance(raw_message, Mapping):
                raise TypeError("DeepSeek response choice missing message mapping.")

            reasoning_content = _string_or_none(raw_message.get(_REASONING_CONTENT_KEY))
            message = generation.message
            if reasoning_content and isinstance(message, AIMessage):
                message.additional_kwargs[_REASONING_CONTENT_KEY] = reasoning_content
                self._reasoning_store.save(message)

        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation_chunk is None:
            return None

        reasoning_content = _reasoning_content_from_stream_chunk(chunk)
        if reasoning_content and isinstance(generation_chunk.message, AIMessageChunk):
            generation_chunk.message.additional_kwargs[_REASONING_CONTENT_KEY] = reasoning_content
        return generation_chunk

    def _hydrate_payload_message(self, payload_message: dict[str, Any]) -> dict[str, Any]:
        if payload_message.get("role") != "assistant":
            return payload_message
        if payload_message.get(_REASONING_CONTENT_KEY):
            return payload_message
        if _TOOL_CALLS_KEY not in payload_message:
            return payload_message

        reasoning_content = self._reasoning_for_payload_message(payload_message)
        if reasoning_content is None:
            return payload_message

        hydrated = dict(payload_message)
        hydrated[_REASONING_CONTENT_KEY] = reasoning_content
        return hydrated

    def _reasoning_for_payload_message(self, payload_message: Mapping[str, Any]) -> str | None:
        message = _ai_message_from_payload(payload_message)
        existing_reasoning = extract_reasoning_content(message)
        if existing_reasoning:
            return existing_reasoning
        return self._reasoning_store.reasoning_for_message(message)


def extract_reasoning_content(message: AIMessage | AIMessageChunk) -> str | None:
    reasoning_content = _string_or_none(message.additional_kwargs.get(_REASONING_CONTENT_KEY))
    if reasoning_content:
        return reasoning_content

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, Mapping):
        return _string_or_none(response_metadata.get(_REASONING_CONTENT_KEY))
    return None


def tool_call_ids_from_message(message: AIMessage) -> list[str]:
    ids: list[str] = []
    for tool_call in message.tool_calls:
        tool_call_id = _tool_call_id(tool_call)
        if tool_call_id:
            ids.append(tool_call_id)

    if not ids:
        raw_tool_calls = message.additional_kwargs.get(_TOOL_CALLS_KEY)
        if isinstance(raw_tool_calls, Sequence) and not isinstance(raw_tool_calls, str | bytes | bytearray):
            for raw_tool_call in raw_tool_calls:
                tool_call_id = _tool_call_id(raw_tool_call)
                if tool_call_id:
                    ids.append(tool_call_id)
    return ids


def _ai_message_from_payload(payload_message: Mapping[str, Any]) -> AIMessage:
    additional_kwargs: dict[str, Any] = {}
    if reasoning_content := _string_or_none(payload_message.get(_REASONING_CONTENT_KEY)):
        additional_kwargs[_REASONING_CONTENT_KEY] = reasoning_content
    if raw_tool_calls := payload_message.get(_TOOL_CALLS_KEY):
        additional_kwargs[_TOOL_CALLS_KEY] = raw_tool_calls

    return AIMessage(
        content=payload_message.get("content") or "",
        additional_kwargs=additional_kwargs,
    )


def _reasoning_content_from_stream_chunk(chunk: Mapping[str, Any]) -> str | None:
    choices = chunk.get("choices", []) or _mapping_get(chunk.get("chunk"), "choices", [])
    if not isinstance(choices, Sequence) or isinstance(choices, str | bytes | bytearray) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise TypeError("DeepSeek stream choice must be a mapping.")
    delta = choice.get("delta")
    if delta is None:
        return None
    if not isinstance(delta, Mapping):
        raise TypeError("DeepSeek stream delta must be a mapping.")
    return _string_or_none(delta.get(_REASONING_CONTENT_KEY))


def _response_to_dict(response: dict | openai.BaseModel) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    return response.model_dump(exclude={"choices": {"__all__": {"message": {"parsed"}}}})


def _tool_call_id(tool_call: object) -> str | None:
    if isinstance(tool_call, Mapping):
        return _string_or_none(tool_call.get("id"))
    return _string_or_none(getattr(tool_call, "id", None))


def _mapping_get(value: object, key: str, default: object) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return default


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "DeepSeekThinkingChatModel",
    "ReasoningSidecarStore",
    "extract_reasoning_content",
    "tool_call_ids_from_message",
]
