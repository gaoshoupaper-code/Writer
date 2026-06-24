"""LLM input 增量存储引擎（Phase 1 · T1/T4/T5/T6/T17）。

核心思想（见设计 T4/T17）：
  同一 trace 内，第 N 次 LLM 调用的 input 通常包含第 N-1 次的 input（历史累积）。
  只存"新增尾部消息"+ 一个指向前次范围的指针，重建时顺着指针往前拼。

去重 key = 消息结构身份（T17），不用内容哈希：
  middleware 的截断（_MAX_TRACE_STRING=10000）会让同一消息的字符串边界微妙变化，
  内容哈希不可靠。改用 type + content 指纹（前缀 hash）+ tool_call_id 等稳定字段。

降级（T6）：
  recorder 重启后内存索引丢失 → 该 trace 后续退化为全量存储（range 为空）。
  不报错、不破坏重建（全量也能重建，只是费空间）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.platform.trace.schemas import TraceContextRange

# content 指纹取前 N 字符计算 hash。选 200：足以区分不同消息，
# 又远小于截断边界 10000，保证截断不会改变指纹（截断保留头尾各 5000，
# 前 200 字符一定在保留的头部内）。
_FINGERPRINT_CHARS = 200


@dataclass
class _SeenMessage:
    """已见消息的记录：结构身份 → 出现位置（anchor + 在 input 列表中的序号）。"""

    anchor_id: str
    index_in_input: int


@dataclass
class IncrementState:
    """单个 trace 的增量计算状态。

    维护"已见消息"索引：结构身份 → 最近一次出现的 (anchor_id, 序号)。
    recorder 跨重启丢失此状态 → 该 trace 退化为全量（T6 降级，安全）。
    """

    # 上一次 LLM input 的完整消息身份列表（按序），用于计算与前次的重复范围。
    # 存身份而非内容：内存占用小，且不依赖内容稳定性。
    last_message_identities: list[str] = field(default_factory=list)
    # 上一次 LLM input 的起始 anchor（range 的 start 锚点）。
    last_input_start_anchor: str | None = None
    # 上一次 LLM input 的结束 anchor（range 的 end 锚点）。
    last_input_end_anchor: str | None = None
    # 已见消息索引：身份 → 最近出现的 _SeenMessage（用于消息级匹配）。
    seen: dict[str, _SeenMessage] = field(default_factory=dict)


@dataclass
class IncrementResult:
    """单次 LLM input 的增量计算结果。"""

    # 写入 event.input 的内容：增量模式下只含新增尾部；全量模式下是完整 input。
    input_to_store: Any
    # 写入 event.input_context_range 的内容：非空=增量，空=None 表示全量（T8）。
    input_context_range: TraceContextRange | None
    # 本事件输出的 anchor_id（由调用方写入 event，T1 稳定）。
    output_anchor_id: str


def message_identity(message: Any) -> str:
    """计算消息的结构身份（T17）。

    不依赖完整内容（截断会破坏），而依赖稳定结构字段：
      type + content 指纹（前 _FINGERPRINT_CHARS 字符的 hash）+ tool_call_id（如有）

    Returns: 身份字符串，相同消息（即使截断后）返回相同身份。
    """
    if isinstance(message, dict):
        msg_type = str(message.get("type") or message.get("role") or "unknown")
        content = message.get("content")
        tool_call_id = message.get("tool_call_id")
    else:
        # 非字典消息（理论上 middleware 已转 dict，防御性处理）
        msg_type = getattr(message, "type", "unknown")
        content = getattr(message, "content", None)
        tool_call_id = getattr(message, "tool_call_id", None)

    # content 指纹：取前 N 字符。截断保留头尾各 5000，前 200 一定在头部内（T17）。
    content_str = "" if content is None else str(content)
    content_fingerprint = hashlib.sha1(content_str[:_FINGERPRINT_CHARS].encode("utf-8")).hexdigest()[:16]

    # tool_call_id 是强标识（同一次工具调用的返回消息 id 唯一），优先纳入。
    tool_component = f"|tc:{tool_call_id}" if tool_call_id else ""

    return f"{msg_type}:{content_fingerprint}{tool_component}"


def extract_messages(input_payload: Any) -> list[Any]:
    """从 LLM input 中提取消息列表。

    middleware 传的 input 形如 {"messages": [...], "system": "..."} 或 {"messages": [...]}。
    system 提示词在 middleware 已拼进 messages 头部（trace_middleware.py:144-148），
    所以这里只取 messages。
    """
    if isinstance(input_payload, dict):
        messages = input_payload.get("messages")
        if isinstance(messages, list):
            return messages
    if isinstance(input_payload, list):
        return input_payload
    return []


def compute_increment(
    state: IncrementState,
    input_payload: Any,
    output_anchor_id: str,
) -> IncrementResult:
    """计算一次 LLM input 的增量表示（T4/T5/T8）。

    Args:
        state: 该 trace 的增量状态（会被原地更新）。
        input_payload: 本次 LLM 的完整 input（middleware 已截断）。
        output_anchor_id: 本次输出的 anchor_id（由 recorder 分配，T1）。

    Returns:
        IncrementResult：input_to_store（写 event.input）、
        input_context_range（写 event.input_context_range，None=全量）。
    """
    messages = extract_messages(input_payload)
    identities = [message_identity(msg) for msg in messages]

    # 首次：无前次可引用 → 全量存储（T8 边界条件1）。
    if not state.last_message_identities:
        state.last_message_identities = identities
        state.last_input_start_anchor = output_anchor_id
        state.last_input_end_anchor = output_anchor_id
        # 记录已见消息（供消息级匹配，虽然首次用不到，保持状态完整）。
        for idx, ident in enumerate(identities):
            state.seen[ident] = _SeenMessage(anchor_id=output_anchor_id, index_in_input=idx)
        return IncrementResult(
            input_to_store=input_payload,
            input_context_range=None,  # 全量
            output_anchor_id=output_anchor_id,
        )

    # 计算与前次的最长公共前缀（前缀匹配：历史消息按序累积，前部通常重复）。
    prev_identities = state.last_message_identities
    common_prefix_len = _common_prefix_length(prev_identities, identities)

    if common_prefix_len == len(identities):
        # 极端：本次 input 完全等于前次（无新增）。存空 input + 引用整个前次范围。
        # 这种情况罕见（通常至少多一条新消息），但需正确处理。
        input_to_store = _rebuild_messages_payload(messages[0:0], input_payload)  # 空消息列表
        ctx_range = TraceContextRange(
            start_anchor_id=state.last_input_start_anchor,
            end_anchor_id=state.last_input_end_anchor,
        )
    elif common_prefix_len == 0:
        # 无公共前缀（input 完全变了，如不同 agent 的首次调用）→ 全量。
        # 不更新 last 状态？不——应该更新，让后续能引用本次。
        state.last_message_identities = identities
        state.last_input_start_anchor = output_anchor_id
        state.last_input_end_anchor = output_anchor_id
        return IncrementResult(
            input_to_store=input_payload,
            input_context_range=None,  # 全量
            output_anchor_id=output_anchor_id,
        )
    else:
        # 正常增量：前 common_prefix_len 条与前次相同（引用），后面是新增尾部。
        new_messages = messages[common_prefix_len:]
        input_to_store = _rebuild_messages_payload(new_messages, input_payload)
        ctx_range = TraceContextRange(
            start_anchor_id=state.last_input_start_anchor,
            end_anchor_id=state.last_input_end_anchor,
        )

    # 更新状态：本次成为下次的"前次"。
    state.last_message_identities = identities
    state.last_input_start_anchor = state.last_input_start_anchor  # range 起点保持链首
    state.last_input_end_anchor = output_anchor_id  # range 终点推进到本次输出

    return IncrementResult(
        input_to_store=input_to_store,
        input_context_range=ctx_range,
        output_anchor_id=output_anchor_id,
    )


def _common_prefix_length(prev: list[str], curr: list[str]) -> int:
    """计算两个身份列表的最长公共前缀长度。"""
    max_len = min(len(prev), len(curr))
    i = 0
    while i < max_len and prev[i] == curr[i]:
        i += 1
    return i


def _rebuild_messages_payload(new_messages: list[Any], original_payload: Any) -> Any:
    """构造增量 input payload。

    保持原 payload 的外层结构（如 {"messages": [...]}），只替换 messages 为新增部分。
    这样 event.input 的结构对所有事件一致（都是 {messages: [...]}），重建端好处理。
    """
    if isinstance(original_payload, dict):
        result = dict(original_payload)
        result["messages"] = new_messages
        return result
    return new_messages


def reconstruct_full_input(
    events: list[Any],
    target_event: Any,
) -> Any:
    """重建某条 LLM 事件的完整 input（D5/T8 重建逻辑）。

    顺着 input_context_range 往前回溯，把增量尾部拼接成完整 input。

    Args:
        events: 该 trace 的所有事件（按 sequence 排序）。
        target_event: 要重建 input 的 LLM 事件（dict 或 TraceLogEvent）。

    Returns: 完整 input（与原始全量 input 同构）。
    """
    # 取 target 的 input 和 range。
    target_input = _get_field(target_event, "input")
    target_range = _get_field(target_event, "input_context_range")

    # range 为空 = 全量（T8），直接返回。
    if target_range is None:
        return target_input

    # range 是 TraceContextRange 或 dict。
    start_anchor = _range_field(target_range, "start_anchor_id")
    end_anchor = _range_field(target_range, "end_anchor_id")

    if start_anchor is None or end_anchor is None:
        return target_input  # 防御：range 不完整，退化为返回存储值

    # 收集从 start_anchor 到 end_anchor 之间所有 LLM 事件的增量尾部（按序）。
    # anchor 链：每次 LLM 事件的 output_anchor_id 标记其输出位置。
    # input_context_range 指向前次的 [start, end] 范围。
    # 重建 = 找到 start_anchor 对应的 LLM 事件（全量起点）→ 沿链拼到 target。
    collected_messages: list[Any] = []
    base_input_found = False

    for event in events:
        event_type = _get_field(event, "type")
        if event_type != "llm_start":
            continue

        event_anchor = _get_field(event, "output_anchor_id") or _get_field(event, "context_anchor_id")
        event_input = _get_field(event, "input")
        event_range = _get_field(event, "input_context_range")

        # 找到链的起点（range 为空 = 全量起点）。
        if not base_input_found:
            if event_range is None:
                # 这是全量起点，它的 input 是完整的。
                # ⚠️ 必须拷贝：extract_messages 返回 event_input["messages"] 的原始
                # list 引用，直接赋值后下面 extend 会污染源 events（连续重建时起点
                # 列表雪球增长 → MemoryError）。与 evolution 端 increment.py 同步。
                collected_messages = list(extract_messages(event_input))
                base_input_found = True
            continue

        # 已找到起点，后续每条都是增量：input.messages 是新增尾部。
        collected_messages.extend(extract_messages(event_input))

        # 到达 target 事件本身时停止（它的 input 已在循环中累加）。
        if _get_field(event, "event_id") == _get_field(target_event, "event_id"):
            break

    if not base_input_found:
        # 链断了（起点找不到，可能是跨重启降级导致）→ 返回存储值（可能是全量或残缺增量）。
        # 这是 T6 降级的边界：尽力而为。
        return target_input

    # 用原始 payload 的外层结构包装重建的 messages。
    if isinstance(target_input, dict):
        result = dict(target_input)
        result["messages"] = collected_messages
        return result
    return collected_messages


def _get_field(obj: Any, field: str) -> Any:
    """从 dict 或对象取字段（兼容 TraceLogEvent 和原始 dict）。"""
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _range_field(range_obj: Any, field: str) -> str | None:
    """从 TraceContextRange 或 dict 取字段。"""
    if range_obj is None:
        return None
    if isinstance(range_obj, dict):
        return range_obj.get(field)
    return getattr(range_obj, field, None)
