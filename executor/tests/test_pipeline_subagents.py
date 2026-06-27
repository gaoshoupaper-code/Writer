from __future__ import annotations

# NOTE: 原测试文件（336 行）测试已废弃的 _build_compiled_pipeline_subagent 管道。
# Pipeline 已被 DeepAgent 架构替代（build_xxx_deep_subagent）。
# 以下测试覆盖新架构的关键组件：RevisionLimitMiddleware 和 ArtifactValidationMiddleware。
# 完整集成测试需要通过前端触发实际创作流程。

import tempfile
import unittest
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

# RevisionLimitMiddleware 已迁进 harness 包（Phase 7），通过包加载后 import
from app.platform.agent.loader import load_current_package
load_current_package()
from harness_current.middleware.revision_limit import RevisionLimitMiddleware
from app.platform.agent.middleware import ArtifactValidationMiddleware


class TestRevisionLimitMiddleware(unittest.TestCase):
    """测试修订次数硬上限中间件。"""

    def _make_request(self, tool_name: str, target: str | None = None) -> Any:
        """构建 mock 的 tool call request 对象。"""
        class MockRequest:
            def __init__(self, name, subagent_type):
                self.tool_call = {
                    "name": name,
                    "args": {"subagent_type": subagent_type} if subagent_type else {},
                    "id": "test-call-1",
                }
        return MockRequest(tool_name, target)

    def test_non_task_tool_passes_through(self):
        """非 task 工具调用应该直接放行。"""
        mw = RevisionLimitMiddleware(max_revisions=3)
        req = self._make_request("write_file")
        result = mw.wrap_tool_call(req, lambda r: "ok")
        self.assertEqual(result, "ok")

    def test_review_call_within_limit(self):
        """review 调用在限制内应该放行。"""
        mw = RevisionLimitMiddleware(max_revisions=3)
        req = self._make_request("task", "review")
        result = mw.wrap_tool_call(req, lambda r: "eval-result")
        self.assertEqual(result, "eval-result")

    def test_review_call_exceeds_limit(self):
        """review 调用超过限制应该返回终止消息。"""
        mw = RevisionLimitMiddleware(max_revisions=2)
        req = self._make_request("task", "review")
        # 前 2 次放行
        mw.wrap_tool_call(req, lambda r: "eval-1")
        mw.wrap_tool_call(req, lambda r: "eval-2")
        # 第 3 次应该被拦截
        result = mw.wrap_tool_call(req, lambda r: "should-not-reach")
        self.assertIsInstance(result, ToolMessage)
        self.assertIn("审查上限", result.content)

    def test_non_review_task_passes_through(self):
        """task 工具但目标不是 review 时应该放行。"""
        mw = RevisionLimitMiddleware(max_revisions=1)
        req = self._make_request("task", "other-subagent")
        result = mw.wrap_tool_call(req, lambda r: "ok")
        self.assertEqual(result, "ok")


class TestArtifactValidationMiddleware(unittest.TestCase):
    """测试产物文件校验中间件。"""

    def test_empty_artifact_paths_always_passes(self):
        """没有配置产物路径时应该始终放行。"""
        mw = ArtifactValidationMiddleware(artifact_paths=[])
        state = {"messages": [AIMessage(content="done")]}
        result = mw.after_model(state, None)
        self.assertIsNone(result)

    def test_tool_call_passes_through(self):
        """AI 消息含工具调用时应该放行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "test.md"
            mw = ArtifactValidationMiddleware(artifact_paths=[artifact])
            state = {"messages": [AIMessage(content="", tool_calls=[{"name": "write_file", "args": {}, "id": "1"}])]}
            result = mw.after_model(state, None)
            self.assertIsNone(result)

    def test_missing_artifact_triggers_block(self):
        """产物文件缺失时应该拦截输出。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "nonexistent.md"
            mw = ArtifactValidationMiddleware(artifact_paths=[artifact])
            state = {"messages": [AIMessage(content="I'm done")]}
            result = mw.after_model(state, None)
            self.assertIsNotNone(result)
            self.assertIn("jump_to", result)


if __name__ == "__main__":
    unittest.main()
