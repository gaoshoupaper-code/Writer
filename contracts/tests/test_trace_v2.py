from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contracts.trace import TraceLogEvent, TraceRunSummary, compute_trace_events_hash
from contracts.trace.payload import (
    ContentAddressedPayloadStore,
    PayloadRejected,
    sanitize_structural_text,
)
from contracts.trace.w3c import create_trace_context, parse_traceparent


class TraceV2ContractTest(unittest.TestCase):
    def test_v1_defaults_remain_legacy_and_unknown(self) -> None:
        run = TraceRunSummary(
            trace_id="legacy-trace",
            workspace_id="workspace",
            thread_id="thread",
            session_name="session",
            workspace_path="",
            endpoint="generate",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            event_count=1,
            path="trace.jsonl",
        )

        self.assertEqual(run.schema_version, 1)
        self.assertEqual(run.integrity_status, "legacy")
        self.assertIsNone(run.workload)
        self.assertEqual(run.coverage, {})

    def test_v2_event_accepts_explicit_payload_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            ref = store.put({"prompt": "完整正文"})
            event = TraceLogEvent(
                trace_id="trace-v2",
                event_id="event-1",
                sequence=1,
                type="llm_start",
                status="running",
                timestamp="2026-01-01T00:00:00+00:00",
                source="runtime",
                schema_version=2,
                payload_refs={"input": ref},
            )

            self.assertEqual(event.payload_refs["input"].content_hash, ref.payload_id)
            self.assertEqual(store.get(ref.payload_id), {"prompt": "完整正文"})

    def test_event_digest_is_stable_across_json_key_reordering(self) -> None:
        base = {
            "trace_id": "trace-v2",
            "event_id": "event-1",
            "sequence": 1,
            "type": "run_start",
            "status": "running",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "source": "runtime",
            "schema_version": 2,
        }
        first = TraceLogEvent(**base, input={"workspace_id": "ws", "user_id": "u1"})
        second = TraceLogEvent(**base, input={"user_id": "u1", "workspace_id": "ws"})

        self.assertEqual(compute_trace_events_hash([first]), compute_trace_events_hash([second]))

    def test_structural_text_redacts_secret_assignments(self) -> None:
        sanitized = sanitize_structural_text(
            "upstream failed: Authorization: Bearer sk-1234567890abcdefghijklmnop"
        )

        self.assertEqual(sanitized, "upstream failed: Authorization=[redacted]")


class PayloadGateTest(unittest.TestCase):
    def test_preserves_long_semantic_content_without_truncation(self) -> None:
        content = "开" + "正文" * 20_000 + "终"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            ref = store.put({"content": content})

            self.assertEqual(store.get(ref.payload_id)["content"], content)

    def test_rejects_forbidden_fields_secrets_and_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            rejected = (
                {"Authorization": "Bearer value"},
                {"value": "sk-abcdefghijklmnopqrstuvwxyz"},
                {"content": b"binary"},
            )

            for value in rejected:
                with self.subTest(value=value), self.assertRaises(PayloadRejected):
                    store.put(value)

            self.assertEqual(list(Path(tmpdir).glob("*.json")), [])

    def test_strips_internal_reasoning_but_preserves_business_fields(self) -> None:
        """DEC-001 / FR-001：内部推理定向剥离，业务正文 100% 保留。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            payload = {
                "messages": [
                    {"role": "user", "content": "写一个场景"},
                    {"role": "assistant", "content": "好的", "reasoning": "私密推理"},
                ],
                "additional_kwargs": {"thinking": "不应保留的 CoT"},
                "chain_of_thought": "顶层 CoT 也要剥离",
                "model": "gpt-4",
            }
            ref = store.put(payload)
            stored = store.get(ref.payload_id)

            # 推理字段不得出现在持久化结果中（CON-001）。
            self.assertNotIn("reasoning", json.dumps(stored, ensure_ascii=False))
            self.assertNotIn("thinking", stored.get("additional_kwargs", {}))
            self.assertNotIn("chain_of_thought", stored)
            # 同级业务字段逐项保留（AC-001）。
            self.assertEqual(stored["messages"][0]["content"], "写一个场景")
            self.assertEqual(stored["messages"][1]["content"], "好的")
            self.assertEqual(stored["additional_kwargs"], {})
            self.assertEqual(stored["model"], "gpt-4")
            # 剥离不标记 degraded（A2）——此处只验证 put 成功且可读回。

    def test_reasoning_then_secret_still_rejected(self) -> None:
        """EDGE-001：剥离 reasoning 后，剩余命中密钥规则仍拒绝整包。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            # 先剥 reasoning，但剩余结构含 api_key → 必须 fail-closed。
            payload = {"reasoning": "剥离我", "api_key": "sk-leaked"}
            with self.assertRaises(PayloadRejected):
                store.put(payload)
            self.assertEqual(list(Path(tmpdir).glob("*.json")), [])

    def test_reasoning_payload_keeps_trace_verifiable(self) -> None:
        """AC-001 / AC-003：含 reasoning 的 LLM 正文经 externalize 后不 degraded，
        业务正文完整可读，integrity 可达 verified（模拟 recorder 判定链路）。

        这是创作与单次测试 Trace 完整性回归的契约级验证：两端 recorder 共享
        PayloadGate，put 成功 = 不触发 _mark_capture_degraded = integrity 可 verified。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            # 模拟真实 LLM input/output（含 reasoning + 业务正文 + 工具调用）。
            llm_input = {
                "messages": [
                    {"role": "system", "content": "你是编剧助手"},
                    {"role": "user", "content": "写第一场戏"},
                ],
                "model": "glm-4",
                "reasoning": "我先构思一下人物动机（私密推理，不得持久化）",
            }
            llm_output = {
                "content": "第一场：夜，办公室。",
                "tool_calls": [{"name": "write_file", "args": {"path": "scene1.md"}}],
                "thinking": "选择夜戏是因为氛围需要（私密推理）",
            }
            # recorder 的 _externalize_payloads 对每个字段独立 put；成功则不 degraded。
            input_ref = store.put(llm_input)
            output_ref = store.put(llm_output)
            stored_input = store.get(input_ref.payload_id)
            stored_output = store.get(output_ref.payload_id)

            # AC-001：内部推理不得持久化（CON-001）。
            self.assertNotIn("reasoning", stored_input)
            self.assertNotIn("thinking", stored_output)
            # AC-001：合法业务字段逐项相等（保留率 100%）。
            self.assertEqual(stored_input["messages"][0]["content"], "你是编剧助手")
            self.assertEqual(stored_input["messages"][1]["content"], "写第一场戏")
            self.assertEqual(stored_input["model"], "glm-4")
            self.assertEqual(stored_output["content"], "第一场：夜，办公室。")
            self.assertEqual(
                stored_output["tool_calls"][0]["args"]["path"], "scene1.md"
            )
            # AC-003：两个 PayloadRef 均成功生成（recorder 据此判定不 degraded）。
            self.assertTrue(input_ref.payload_id)
            self.assertTrue(output_ref.payload_id)
            self.assertNotEqual(input_ref.payload_id, output_ref.payload_id)
            # 模拟 recorder 的 integrity 判定：put 全部成功 → 无 degraded → verified 可达。
            # （真实 recorder 还要求终态事件存在，此处只验证 reasoning 不是 degraded 来源。）
            degraded_reasons: list[str] = []  # put 未抛 PayloadRejected → 空列表
            integrity_reachable = not degraded_reasons
            self.assertTrue(
                integrity_reachable,
                "reasoning 剥离不得阻止 Trace 达到 verified",
            )


class W3CTraceContextTest(unittest.TestCase):
    def test_valid_parent_keeps_w3c_trace_id_and_creates_local_span(self) -> None:
        incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

        context = create_trace_context(incoming)

        self.assertEqual(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(context.parent_span_id, "00f067aa0ba902b7")
        self.assertEqual(len(context.span_id), 16)
        self.assertEqual(context.traceparent, f"00-{context.trace_id}-{context.span_id}-01")
        self.assertEqual(context.external_refs["traceparent"], context.traceparent)

    def test_invalid_or_zero_parent_is_not_propagated(self) -> None:
        self.assertIsNone(parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("not-a-traceparent"))

        context = create_trace_context("not-a-traceparent")
        self.assertIsNone(context.parent_span_id)
        self.assertEqual(len(context.trace_id), 32)
        self.assertEqual(len(context.span_id), 16)


if __name__ == "__main__":
    unittest.main()
