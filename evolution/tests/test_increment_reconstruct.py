"""增量 input 重建的回归测试。

锁定历史上的一次严重 bug：reconstruct_full_input 把起点（全量）事件的
input.messages 原始 list 引用直接赋给 collected_messages，后续 extend 会
污染源 events。进化端详情视图对同一条 trace 连续为每个 llm_start 调用重建，
每次都把起点列表撑大 → 指数膨胀 → MemoryError → Internal Server Error。

修复有两层：
1. increment.reconstruct_full_input：起点赋值改为 list(...) 拷贝，杜绝副作用。
2. traces._reconstruct_incremental_inputs：先算后写，避免边重建边污染遍历源。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.increment import reconstruct_full_input


def _msg(role: str, content: str) -> dict:
    return {"type": role, "content": content}


def _input(messages: list) -> dict:
    return {"messages": messages}


def _llm_event(
    event_id: str,
    sequence: int,
    messages: list,
    anchor: str,
    input_range: dict | None,
) -> dict:
    return {
        "event_id": event_id,
        "sequence": sequence,
        "type": "llm_start",
        "input": _input(messages),
        "output_anchor_id": anchor,
        "input_context_range": input_range,
    }


class ReconstructNoSideEffectTest(unittest.TestCase):
    """重建不得污染传入的 events（历史 MemoryError 根因）。"""

    def test_single_call_does_not_mutate_source(self) -> None:
        """单次重建后，起点事件的 input.messages 必须保持原样。"""
        events = [
            _llm_event("e1", 1, [_msg("system", "sys"), _msg("human", "q1")], "a1", None),
            _llm_event("e2", 2, [_msg("ai", "a1"), _msg("human", "q2")], "a2",
                       {"start_anchor_id": "a1", "end_anchor_id": "a1"}),
            _llm_event("e3", 3, [_msg("ai", "a2")], "a3",
                       {"start_anchor_id": "a1", "end_anchor_id": "a2"}),
        ]
        original_e1 = list(events[0]["input"]["messages"])

        reconstruct_full_input(events, events[2])

        # 修复前：e1 的 messages 会被 extend 成 5 条（副作用）。
        self.assertEqual([m["content"] for m in events[0]["input"]["messages"]],
                         [m["content"] for m in original_e1])
        self.assertEqual(len(events[0]["input"]["messages"]), 2)

    def test_consecutive_reconstructs_stay_linear(self) -> None:
        """连续重建整条链：每条结果线性增长，源数据始终不变。

        旧实现下第二次调用就会读到被污染的起点，结果雪球膨胀。
        """
        events = [
            _llm_event("e1", 1, [_msg("system", "sys"), _msg("human", "q1")], "a1", None),
            _llm_event("e2", 2, [_msg("ai", "a1"), _msg("human", "q2")], "a2",
                       {"start_anchor_id": "a1", "end_anchor_id": "a1"}),
            _llm_event("e3", 3, [_msg("ai", "a2")], "a3",
                       {"start_anchor_id": "a1", "end_anchor_id": "a2"}),
            _llm_event("e4", 4, [_msg("ai", "a3")], "a4",
                       {"start_anchor_id": "a1", "end_anchor_id": "a3"}),
        ]
        snapshot = [len(e["input"]["messages"]) for e in events]

        sizes = []
        for event in events:
            if event["input_context_range"] is None:
                sizes.append(len(event["input"]["messages"]))
                continue
            full = reconstruct_full_input(events, event)
            sizes.append(len(full["messages"]))

        # 线性：2 / 4 / 5 / 6。旧 bug 下会是 37/37/37/37（4 个事件就指数爆炸）。
        self.assertEqual(sizes, [2, 4, 5, 6])
        # 源 events 未被任何一次重建改动。
        self.assertEqual([len(e["input"]["messages"]) for e in events], snapshot)


class ReconstructCorrectnessTest(unittest.TestCase):
    """重建内容正确性（与执行端 test_trace_increment 对齐）。"""

    def test_reconstruct_incremental_chain(self) -> None:
        events = [
            _llm_event("e1", 1, [_msg("system", "sys"), _msg("human", "q1")], "a1", None),
            _llm_event("e2", 2, [_msg("ai", "a1"), _msg("human", "q2")], "a2",
                       {"start_anchor_id": "a1", "end_anchor_id": "a1"}),
            _llm_event("e3", 3, [_msg("ai", "a2")], "a3",
                       {"start_anchor_id": "a1", "end_anchor_id": "a2"}),
        ]
        full = reconstruct_full_input(events, events[2])
        msgs = full["messages"]
        self.assertEqual([m["content"] for m in msgs], ["sys", "q1", "a1", "q2", "a2"])

    def test_full_event_returns_as_is(self) -> None:
        event = _llm_event("e1", 1, [_msg("system", "sys")], "a1", None)
        full = reconstruct_full_input([event], event)
        self.assertEqual(full["messages"][0]["content"], "sys")


if __name__ == "__main__":
    unittest.main()
