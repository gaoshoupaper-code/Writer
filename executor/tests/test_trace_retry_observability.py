"""TraceMiddleware 模型重试可观测测试（FR-003 / EVD-006 / EDGE-002）。

验证开启 retry_budget 后，TraceMiddleware 把 SDK 内部的多次传输尝试变成可见的
middleware_intervention 事件（attempt 失败 + 退避），且最终 llm_error 携带预算信息。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.platform.agent.middleware import TraceMiddleware
from app.platform.trace.recorder import TraceRecorder
from app.schemas.screenplay import ThreadSummary


def _thread(workspace: Path) -> ThreadSummary:
    return ThreadSummary(
        thread_id="thread-retry", workspace_id="ws", session_name="s",
        workspace_path=str(workspace), created_at="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
    )


def _interventions(recorder: TraceRecorder, thread: ThreadSummary, trace_id: str) -> list[dict]:
    detail = recorder.read_run(thread, trace_id)
    assert detail is not None
    out = []
    for ev in detail.events:
        if ev.type == "middleware_intervention" and ev.intervention:
            out.append(ev.intervention)
    return out


def _make_runner():
    """用领域层工厂构建 retry_runner（与生产 agent.py _make_retry_runner_factory 同源）。"""
    from app.domains.writing.agent import _make_retry_runner_factory

    return _make_retry_runner_factory()()


def _make_request():
    """构造一个干净的 ModelRequest stub（避免 MagicMock 自动属性污染 payload 序列化）。"""
    req = MagicMock()
    req.system_message = None  # 关键：MagicMock 默认会造出 truthy system_message
    req.messages = []
    req.model = MagicMock()
    req.model.model_name = "glm-5.2"
    return req


class TraceRetryObservabilityTest(unittest.TestCase):
    def test_retry_attempts_become_visible_interventions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            thread = _thread(workspace)
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate.stream")
            mw = TraceMiddleware(
                recorder, handle.trace_id, "writing-subagent", retry_runner=_make_runner(),
            )

            calls = []

            async def runner():
                async def _handler(_req):
                    calls.append(1)
                    # 首次连续无响应超时，第 2 次 attempt 成功（总预算 = 2）。
                    if len(calls) < 2:
                        raise _make_exc("APITimeoutError")
                    return _make_response()

                await mw.awrap_model_call(_make_request(), _handler)

            asyncio.run(runner())

            # 精确 2 次传输尝试：1 次 attempt 失败 intervention + 1 次退避 intervention。
            interventions = _interventions(recorder, thread, handle.trace_id)
            attempt_fails = [i for i in interventions if i.get("action") == "model_attempt_failed"]
            backoffs = [i for i in interventions if i.get("action") == "model_attempt_backoff"]
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(attempt_fails), 1)
            self.assertEqual(len(backoffs), 1)
            # 失败原因携带 attempt 进度（attempt 1/2）。
            self.assertIn("attempt 1/2", attempt_fails[0]["reason"])

    def test_non_retryable_failure_records_single_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            thread = _thread(workspace)
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate.stream")
            mw = TraceMiddleware(
                recorder, handle.trace_id, "writing-subagent", retry_runner=_make_runner(),
            )

            calls = []

            async def runner():
                async def _handler(_req):
                    calls.append(1)
                    raise _make_exc("AuthenticationError")

                with self.assertRaises(Exception):
                    await mw.awrap_model_call(_make_request(), _handler)

            asyncio.run(runner())

            # 认证错误：精确 1 次 attempt，1 条失败 intervention，无退避。
            self.assertEqual(len(calls), 1)
            interventions = _interventions(recorder, thread, handle.trace_id)
            fails = [i for i in interventions if i.get("action") == "model_attempt_failed"]
            backoffs = [i for i in interventions if i.get("action") == "model_attempt_backoff"]
            self.assertEqual(len(fails), 1)
            self.assertEqual(len(backoffs), 0)

    def test_no_retry_runner_keeps_legacy_single_boundary(self) -> None:
        # 未注入 retry_runner：保持原"只看外边界"行为，不写 attempt intervention。
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            thread = _thread(workspace)
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate.stream")
            mw = TraceMiddleware(recorder, handle.trace_id, "writing-subagent")  # 无 retry_runner

            async def runner():
                async def _handler(_req):
                    return _make_response()

                await mw.awrap_model_call(_make_request(), _handler)

            asyncio.run(runner())
            interventions = _interventions(recorder, thread, handle.trace_id)
            self.assertEqual(interventions, [])


def _make_response():
    """构造一个属性有限的响应 stub（避免 MagicMock 自动属性让 usage 提取无限递归）。

    用 SimpleNamespace 而非 MagicMock：_usage_payload 会 getattr 遍历响应树，
    MagicMock 的自动属性会造成无限递归；SimpleNamespace 只暴露显式声明的字段。
    """
    from types import SimpleNamespace

    result = SimpleNamespace(messages=[])
    return SimpleNamespace(result=result, messages=[], structured_response=None)


def _make_exc(name: str) -> BaseException:
    return type(name, (Exception,), {})("simulated")


if __name__ == "__main__":
    unittest.main()
