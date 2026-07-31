"""FR-005 / EVD-004：确定性流水线 llm 观测桥的 output schema 必须与
TraceMiddleware 的 {messages: [...]} 契约对齐，否则 evolution 自有 LLM 调用节点
在前端抽屉里读 output.messages 会落空（前端只认 messages 键）。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.trace.observers import TraceLlmObserver


class TraceLlmObserverSchemaTest(unittest.TestCase):
    def _make_observer(self) -> tuple[TraceLlmObserver, MagicMock]:
        recorder = MagicMock()
        observer = TraceLlmObserver(
            recorder, "trace-observer-schema", component="dossier-compiler"
        )
        return observer, recorder

    def test_on_llm_end_output_has_messages_key(self) -> None:
        observer, recorder = self._make_observer()
        observer.on_llm_end(
            phase="contract_parse", model="deepseek-chat",
            duration_ms=12.3, output="主角踏上寻剑之旅。",
        )
        emitted = recorder.append_event.call_args.args[1]
        self.assertEqual(emitted["type"], "llm_end")
        output = emitted["output"]
        self.assertIsInstance(output, dict)
        self.assertIn("messages", output)
        messages = output["messages"]
        self.assertIsInstance(messages, list)
        self.assertTrue(messages, "messages 不能为空")
        # 最后一条应是 ai 消息且带 content（前端 extractLlmText 取 ai 消息 content）
        self.assertEqual(messages[-1].get("type"), "ai")
        self.assertEqual(messages[-1].get("content"), "主角踏上寻剑之旅。")

    def test_on_llm_start_input_keeps_messages_key(self) -> None:
        """回归：input 侧本就有 messages 键，改 output schema 时不能破坏 input。"""
        observer, recorder = self._make_observer()
        observer.on_llm_start(
            phase="contract_parse", model="deepseek-chat",
            messages=[{"role": "user", "content": "解析契约"}],
        )
        emitted = recorder.append_event.call_args.args[1]
        self.assertEqual(emitted["type"], "llm_start")
        self.assertIn("messages", emitted["input"])

    def test_on_llm_error_does_not_emit_output(self) -> None:
        """回归：error 事件不应带 output 字段，避免误读为成功产出。"""
        observer, recorder = self._make_observer()
        observer.on_llm_error(
            phase="contract_parse", model="deepseek-chat",
            duration_ms=5.0, error=RuntimeError("timeout"),
        )
        emitted = recorder.append_event.call_args.args[1]
        self.assertEqual(emitted["type"], "llm_error")
        self.assertNotIn("output", emitted)


if __name__ == "__main__":
    unittest.main()
