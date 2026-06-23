"""Phase 1 增量存储测试（T1/T4/T5/T6/T8）。

覆盖：
- 首条 LLM 事件全量存储（T8 边界1）
- 后续事件增量存储 + range 指针（T4/T5）
- 完全重复 input 的极端处理
- 跨重启降级：状态丢失后退化为全量（T6）
- reconstruct_full_input 完整重建（D5）
"""

from __future__ import annotations

import unittest

from app.platform.trace.increment import (
    IncrementState,
    compute_increment,
    message_identity,
    reconstruct_full_input,
)


def _llm_event(
    event_id: str,
    sequence: int,
    input_payload: dict | None,
    output_anchor_id: str | None = None,
    input_context_range: dict | None = None,
) -> dict:
    """构造一个 llm_start 事件 dict（模拟写盘后的形态）。"""
    return {
        "type": "llm_start",
        "event_id": event_id,
        "sequence": sequence,
        "input": input_payload,
        "output_anchor_id": output_anchor_id,
        "input_context_range": input_context_range,
    }


def _input(messages: list[dict]) -> dict:
    return {"messages": messages}


def _msg(msg_type: str, content: str, tool_call_id: str | None = None) -> dict:
    msg = {"type": msg_type, "content": content}
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    return msg


class MessageIdentityTest(unittest.TestCase):
    """T17：结构身份去重，不依赖完整内容。"""

    def test_same_content_same_identity(self) -> None:
        m1 = _msg("system", "你是写作助手" + "x" * 20000)
        m2 = _msg("system", "你是写作助手" + "x" * 20000)
        self.assertEqual(message_identity(m1), message_identity(m2))

    def test_truncated_content_same_identity(self) -> None:
        """截断后前 200 字符相同 → 身份相同（T17 核心）。"""
        long = "A" * 5000 + "B" * 5000  # 完整
        truncated = "A" * 5000 + "…[truncated]…" + "B" * 5000  # 截断后
        # 前 200 字符都是 "A"，身份相同
        m1 = _msg("system", long)
        m2 = _msg("system", truncated)
        self.assertEqual(message_identity(m1), message_identity(m2))

    def test_different_content_different_identity(self) -> None:
        m1 = _msg("human", "写第一章")
        m2 = _msg("human", "写第二章")
        self.assertNotEqual(message_identity(m1), message_identity(m2))

    def test_tool_call_id_distinguishes(self) -> None:
        """tool_call_id 是强标识，相同 content 不同 id → 不同身份。"""
        m1 = _msg("tool", "result", tool_call_id="call-1")
        m2 = _msg("tool", "result", tool_call_id="call-2")
        self.assertNotEqual(message_identity(m1), message_identity(m2))


class IncrementComputeTest(unittest.TestCase):
    """T4/T5/T8：增量计算核心。"""

    def test_first_event_is_full(self) -> None:
        """首条 LLM 事件无前次可引用 → 全量存储（T8 边界1）。"""
        state = IncrementState()
        result = compute_increment(state, _input([_msg("system", "sys"), _msg("human", "hi")]), "anchor-1")
        self.assertIsNone(result.input_context_range)  # 全量
        self.assertEqual(len(result.input_to_store["messages"]), 2)

    def test_second_event_incremental(self) -> None:
        """第二条：前2条重复（引用range），只存新增尾部（T4/T5）。"""
        state = IncrementState()
        # 第一次
        compute_increment(state, _input([_msg("system", "sys"), _msg("human", "q1")]), "anchor-1")
        # 第二次：多了一条 ai 回复 + 新 human
        result = compute_increment(
            state,
            _input([_msg("system", "sys"), _msg("human", "q1"), _msg("ai", "a1"), _msg("human", "q2")]),
            "anchor-2",
        )
        self.assertIsNotNone(result.input_context_range)  # 增量
        self.assertEqual(result.input_context_range.start_anchor_id, "anchor-1")
        # input 只存新增的 2 条
        self.assertEqual(len(result.input_to_store["messages"]), 2)
        self.assertEqual(result.input_to_store["messages"][0]["content"], "a1")

    def test_no_common_prefix_is_full(self) -> None:
        """无公共前缀（input 完全变了）→ 全量。"""
        state = IncrementState()
        compute_increment(state, _input([_msg("system", "sys-A")]), "anchor-1")
        result = compute_increment(state, _input([_msg("system", "sys-B")]), "anchor-2")
        self.assertIsNone(result.input_context_range)  # 全量

    def test_empty_after_prefix(self) -> None:
        """本次 input 完全等于前次（无新增）→ 存空消息 + 引用整个前次范围。"""
        state = IncrementState()
        payload = _input([_msg("system", "sys"), _msg("human", "q1")])
        compute_increment(state, payload, "anchor-1")
        result = compute_increment(state, payload, "anchor-2")
        self.assertIsNotNone(result.input_context_range)
        self.assertEqual(len(result.input_to_store["messages"]), 0)


class RestartDegradeTest(unittest.TestCase):
    """T6：跨重启降级。状态丢失后新 state 从空开始 → 退化为全量。"""

    def test_lost_state_degrades_to_full(self) -> None:
        # 模拟：第一条用 state-A，重启后 state-B 为空（新实例）
        state_a = IncrementState()
        compute_increment(state_a, _input([_msg("system", "sys"), _msg("human", "q1")]), "anchor-1")

        # 重启：state_b 是全新空的
        state_b = IncrementState()
        result = compute_increment(
            state_b,
            _input([_msg("system", "sys"), _msg("human", "q1"), _msg("human", "q2")]),
            "anchor-3",
        )
        # 退化全量：range 为空，input 是完整内容
        self.assertIsNone(result.input_context_range)
        self.assertEqual(len(result.input_to_store["messages"]), 3)


class ReconstructTest(unittest.TestCase):
    """D5/T8：完整重建。顺着 anchor 链回溯拼出完整 input。"""

    def test_reconstruct_incremental_chain(self) -> None:
        """三条 LLM 事件链：全量 → 增量 → 增量，重建第三条的完整 input。"""
        events = [
            _llm_event("e1", 1, _input([_msg("system", "sys"), _msg("human", "q1")]), "anchor-1", None),
            _llm_event("e2", 2, _input([_msg("ai", "a1"), _msg("human", "q2")]), "anchor-2",
                       {"start_anchor_id": "anchor-1", "end_anchor_id": "anchor-1"}),
            _llm_event("e3", 3, _input([_msg("ai", "a2")]), "anchor-3",
                       {"start_anchor_id": "anchor-1", "end_anchor_id": "anchor-2"}),
        ]
        full = reconstruct_full_input(events, events[2])
        msgs = full["messages"]
        # 重建结果 = e1全量(2条) + e2增量(2条) + e3增量(1条) = 5条
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[0]["content"], "sys")
        self.assertEqual(msgs[4]["content"], "a2")

    def test_reconstruct_full_event_returns_as_is(self) -> None:
        """range 为空（全量）的事件，重建直接返回其 input。"""
        event = _llm_event("e1", 1, _input([_msg("system", "sys")]), "anchor-1", None)
        full = reconstruct_full_input([event], event)
        self.assertEqual(full["messages"][0]["content"], "sys")


if __name__ == "__main__":
    unittest.main()
