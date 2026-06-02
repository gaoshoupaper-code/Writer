import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk

from app.writer.deepseek_thinking import DeepSeekThinkingChatModel, ReasoningSidecarStore
from app.writer.models import build_writer_model, parse_writer_model


class WriterModelTest(unittest.TestCase):
    def test_parse_writer_model_defaults_to_openai_provider(self) -> None:
        self.assertEqual(parse_writer_model("gpt-4o-mini"), ("openai", "gpt-4o-mini"))

    def test_parse_writer_model_detects_bare_deepseek_model_names(self) -> None:
        self.assertEqual(parse_writer_model("deepseek-v4-pro"), ("deepseek", "deepseek-v4-pro"))

    def test_parse_writer_model_rejects_incomplete_provider_prefix(self) -> None:
        for raw_model in ("deepseek:", ":deepseek-chat"):
            with self.subTest(raw_model=raw_model):
                with self.assertRaises(ValueError):
                    parse_writer_model(raw_model)

    def test_build_writer_model_uses_deepseek_adapter_for_prefixed_model(self) -> None:
        model = build_writer_model(
            SimpleNamespace(
                writer_model="deepseek:deepseek-chat",
                writer_temperature=None,
                writer_top_p=None,
                openai_api_key="test-key",
                openai_base_url="https://api.deepseek.com",
            )
        )

        self.assertIsInstance(model, DeepSeekThinkingChatModel)
        self.assertEqual(model.model_name, "deepseek-chat")
        self.assertEqual(model.extra_body, {"thinking": {"type": "enabled"}})

    def test_build_writer_model_uses_deepseek_adapter_for_bare_model(self) -> None:
        model = build_writer_model(
            SimpleNamespace(
                writer_model="deepseek-v4-pro",
                writer_temperature=None,
                writer_top_p=None,
                openai_api_key="test-key",
                openai_base_url="https://api.deepseek.com",
            )
        )

        self.assertIsInstance(model, DeepSeekThinkingChatModel)
        self.assertEqual(model.model_name, "deepseek-v4-pro")
        self.assertEqual(model.extra_body, {"thinking": {"type": "enabled"}})

    def test_build_writer_model_omits_thinking_for_non_deepseek_models(self) -> None:
        model = build_writer_model(
            SimpleNamespace(
                writer_model="openai:gpt-4o-mini",
                writer_temperature=None,
                writer_top_p=None,
                openai_api_key="test-key",
                openai_base_url="https://api.openai.com/v1",
            )
        )

        self.assertEqual(model.model_name, "gpt-4o-mini")
        self.assertIsNone(model.extra_body)


class DeepSeekThinkingModelTest(unittest.TestCase):
    def test_sidecar_saves_reasoning_by_tool_call_id(self) -> None:
        store = ReasoningSidecarStore()
        message = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "need a tool"},
            tool_calls=[{"name": "lookup", "args": {}, "id": "call_1"}],
        )

        store.save(message)

        stripped_message = AIMessage(
            content="",
            tool_calls=[{"name": "lookup", "args": {}, "id": "call_1"}],
        )
        self.assertEqual(store.reasoning_for_message(stripped_message), "need a tool")

    def test_sidecar_ignores_reasoning_without_tool_calls(self) -> None:
        store = ReasoningSidecarStore()
        store.save(AIMessage(content="done", additional_kwargs={"reasoning_content": "private"}))

        self.assertIsNone(store.reasoning_for_message(AIMessage(content="done")))

    def test_chat_result_preserves_deepseek_reasoning(self) -> None:
        model = DeepSeekThinkingChatModel(
            model="deepseek-chat",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            extra_body={"thinking": {"type": "enabled"}},
            stream_usage=False,
        )

        result = model._create_chat_result(
            {
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "need a tool",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            }
        )

        message = result.generations[0].message
        self.assertEqual(message.additional_kwargs["reasoning_content"], "need a tool")
        stripped_payload = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        }
        self.assertEqual(model._hydrate_payload_message(stripped_payload)["reasoning_content"], "need a tool")

    def test_request_payload_preserves_existing_reasoning_from_messages(self) -> None:
        model = DeepSeekThinkingChatModel(
            model="deepseek-chat",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            extra_body={"thinking": {"type": "enabled"}},
            stream_usage=False,
        )
        message = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "need a tool"},
            tool_calls=[{"name": "lookup", "args": {}, "id": "call_1"}],
        )

        payload = model._get_request_payload([message])

        self.assertEqual(payload["messages"][0]["reasoning_content"], "need a tool")

    def test_stream_chunk_preserves_reasoning_delta(self) -> None:
        model = DeepSeekThinkingChatModel(
            model="deepseek-chat",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            extra_body={"thinking": {"type": "enabled"}},
            stream_usage=False,
        )

        chunk = model._convert_chunk_to_generation_chunk(
            {"choices": [{"delta": {"role": "assistant", "reasoning_content": "step"}}]},
            AIMessageChunk,
            None,
        )

        self.assertIsNotNone(chunk)
        self.assertEqual(chunk.message.additional_kwargs["reasoning_content"], "step")


if __name__ == "__main__":
    unittest.main()
