from __future__ import annotations

import unittest

from app.dossier.recovery import (
    ReadObservation,
    ToolInterval,
    TracePayloadRecoveryError,
    _collect_intervals,
    reconstruct_artifact_heads,
)
from app.core.models import TraceLogEvent


def _operation(
    event_id: str,
    start: int,
    end: int,
    tool_name: str,
    args: dict,
    path: str = "/chapter/chapter-01.md",
) -> ToolInterval:
    return ToolInterval(
        event_id=event_id,
        start_sequence=start,
        end_sequence=end,
        tool_call_id=f"call-{event_id}",
        tool_name=tool_name,
        agent_name="writing-subagent",
        path=path,
        args=args,
        payload_ids=(f"payload-{event_id}",),
    )


class TracePayloadRecoveryTest(unittest.TestCase):
    def test_interval_order_recovers_one_final_head(self) -> None:
        heads = reconstruct_artifact_heads(
            [
                _operation("write", 1, 4, "write_file", {"content": "A\nB\n"}),
                _operation(
                    "edit",
                    5,
                    8,
                    "edit_file",
                    {"old_string": "B", "new_string": "C"},
                ),
            ],
            [],
        )

        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0].content, "A\nC\n")
        self.assertEqual(heads[0].final_operation.event_id, "edit")

    def test_overlapping_operations_must_converge_to_one_hash(self) -> None:
        operations = [
            _operation("write", 1, 2, "write_file", {"content": "A B"}),
            _operation("edit-a", 3, 8, "edit_file", {"old_string": "A", "new_string": "X"}),
            _operation("edit-b", 4, 7, "edit_file", {"old_string": "B", "new_string": "Y"}),
        ]

        head = reconstruct_artifact_heads(operations, [])[0]

        self.assertEqual(head.content, "X Y")

    def test_ambiguous_overlapping_edits_are_rejected(self) -> None:
        operations = [
            _operation("write", 1, 2, "write_file", {"content": "AB"}),
            _operation("edit-a", 3, 8, "edit_file", {"old_string": "A", "new_string": "B"}),
            _operation(
                "edit-b", 4, 7, "edit_file",
                {"old_string": "B", "new_string": "C", "replace_all": True},
            ),
        ]

        with self.assertRaises(TracePayloadRecoveryError):
            reconstruct_artifact_heads(operations, [])

    def test_multiple_overwrite_writes_recover_last_content(self) -> None:
        """同一文件多次 write_file 全量覆盖写（非 edit），取最后一次的 content。

        真实创作中 agent 可能对同一文件多次 write_file 覆盖（而非 write+edit）。
        旧逻辑只接受首次 write 导致这类 trace 恢复失败；修复后 write_file 是
        覆盖语义，最终内容 = sequence 最后一次 write 的 content。
        """
        operations = [
            _operation("write-1", 1, 2, "write_file", {"content": "第一版"}),
            _operation("write-2", 3, 4, "write_file", {"content": "第二版"}),
            _operation("write-3", 5, 6, "write_file", {"content": "第三版"}),
        ]

        head = reconstruct_artifact_heads(operations, [])[0]

        self.assertEqual(head.content, "第三版")
        self.assertEqual(head.final_operation.event_id, "write-3")

    def test_write_then_overwrite_then_edit_recover_correctly(self) -> None:
        """混合序列：write → 覆盖 write → edit，最终基于第二次 write 的内容做 edit。"""
        operations = [
            _operation("write-1", 1, 2, "write_file", {"content": "旧内容"}),
            _operation("write-2", 3, 4, "write_file", {"content": "基础正文"}),
            _operation(
                "edit", 5, 6, "edit_file",
                {"old_string": "基础", "new_string": "最终"},
            ),
        ]

        head = reconstruct_artifact_heads(operations, [])[0]

        self.assertEqual(head.content, "最终正文")

    def test_read_observation_constrains_an_overlapping_edit_order(self) -> None:
        operations = [
            _operation("write", 1, 2, "write_file", {"content": "AB"}),
            _operation("edit-a", 3, 8, "edit_file", {"old_string": "A", "new_string": "B"}),
            _operation(
                "edit-b", 4, 7, "edit_file",
                {"old_string": "B", "new_string": "C", "replace_all": True},
            ),
        ]
        observation = ReadObservation(
            start_sequence=8,
            end_sequence=9,
            path="/chapter/chapter-01.md",
            lines={1: "BC"},
            event_id="read",
            payload_ids=("payload-read",),
        )

        head = reconstruct_artifact_heads(operations, [observation])[0]

        self.assertEqual(head.content, "BC")
        self.assertIn("read", head.support_event_ids)

    def test_tool_message_error_is_not_recovered_as_completed_write(self) -> None:
        events = [
            TraceLogEvent(
                trace_id="trace-error",
                event_id="start",
                sequence=1,
                type="tool_start",
                status="running",
                timestamp="2026-07-30T00:00:00+00:00",
                source="middleware",
                tool_call_id="call-error",
                tool_name="write_file",
            ),
            TraceLogEvent(
                trace_id="trace-error",
                event_id="end",
                sequence=2,
                type="tool_end",
                status="completed",
                timestamp="2026-07-30T00:00:01+00:00",
                source="middleware",
                tool_call_id="call-error",
                tool_name="write_file",
                tool_args={"file_path": "/chapter/chapter-01.md", "content": "正文"},
                tool_output={"status": "error", "error": "disk full"},
            ),
        ]

        operations, observations = _collect_intervals(events)

        self.assertEqual(operations, [])
        self.assertEqual(observations, [])


if __name__ == "__main__":
    unittest.main()
