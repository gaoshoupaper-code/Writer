"""LLM input 重建引擎（evolution 侧 · Phase 3 T3.3）。

与后端 platform/trace/increment.py 的重建逻辑对应。evolution 摄入时
**保持增量存储**（D4/D9 永久保留需控空间），投影/评分需要完整 input 时
按需重建（顺着 anchor 链回溯拼接）。

重建逻辑（D5/T8）：
  input_context_range 为空 → 全量，直接返回 event.input
  input_context_range 非空 → 增量，顺着 range 往前回溯拼接
"""

from __future__ import annotations

from typing import Any


def extract_messages(input_payload: Any) -> list[Any]:
    """从 LLM input 中提取消息列表（与后端 increment.py 一致）。"""
    if isinstance(input_payload, dict):
        messages = input_payload.get("messages")
        if isinstance(messages, list):
            return messages
    if isinstance(input_payload, list):
        return input_payload
    return []


def reconstruct_full_input(
    events: list[Any],
    target_event: Any,
) -> Any:
    """重建某条 LLM 事件的完整 input（D5/T8 重建逻辑）。

    与后端 increment.py 的 reconstruct_full_input 行为一致：
    顺着 input_context_range 往前回溯，把增量尾部拼接成完整 input。

    Args:
        events: 该 trace 的所有事件（必须按 sequence 排序）。
        target_event: 要重建 input 的 LLM 事件（dict 或 TraceLogEvent）。

    Returns: 完整 input（与原始全量 input 同构）。
    """
    target_input = _get_field(target_event, "input")
    target_range = _get_field(target_event, "input_context_range")

    # range 为空 = 全量（T8），直接返回。
    if target_range is None:
        return target_input

    start_anchor = _range_field(target_range, "start_anchor_id")
    end_anchor = _range_field(target_range, "end_anchor_id")

    if start_anchor is None or end_anchor is None:
        return target_input  # 防御：range 不完整

    collected_messages: list[Any] = []
    base_input_found = False

    for event in events:
        event_type = _get_field(event, "type")
        if event_type != "llm_start":
            continue

        event_range = _get_field(event, "input_context_range")
        event_input = _get_field(event, "input")

        # 找到链的起点（range 为空 = 全量起点）。
        if not base_input_found:
            if event_range is None:
                collected_messages = extract_messages(event_input)
                base_input_found = True
            continue

        # 已找到起点，后续每条都是增量。
        collected_messages.extend(extract_messages(event_input))

        # 到达 target 事件本身时停止。
        if _get_field(event, "event_id") == _get_field(target_event, "event_id"):
            break

    if not base_input_found:
        return target_input  # 链断（T6 降级边界），尽力而为

    if isinstance(target_input, dict):
        result = dict(target_input)
        result["messages"] = collected_messages
        return result
    return collected_messages


def _get_field(obj: Any, field: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _range_field(range_obj: Any, field: str) -> str | None:
    if range_obj is None:
        return None
    if isinstance(range_obj, dict):
        return range_obj.get(field)
    return getattr(range_obj, field, None)
