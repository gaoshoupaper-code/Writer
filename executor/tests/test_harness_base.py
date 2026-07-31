"""Phase 1 T1.1：harness 基类契约测试（D16 契约化 Python）。

覆盖：
- WriterHarness 抽象：不能直接实例化，子类必须实现所有 abstractmethod
- SubagentHarness：is_deep 默认 False，build_spec 默认组装
- SubagentHarness deep 分支：is_deep=True 时必须实现 build_deep_params
- HarnessContext 字段默认值
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.platform.harness import HarnessContext, SubagentHarness, WriterHarness


# ── HarnessContext ──────────────────────────────────────────


class TestHarnessContext:
    def test_defaults(self) -> None:
        ctx = HarnessContext(workspace_path=Path("/tmp/ws"))
        assert ctx.workspace_path == Path("/tmp/ws")
        assert ctx.trace_id is None
        assert ctx.owner_id is None
        assert ctx.meta_style is None

    def test_all_fields(self) -> None:
        ctx = HarnessContext(
            workspace_path=Path("/tmp/ws"),
            trace_id="t1",
            owner_id="u1",
            workspace_id="w1",
            meta_style="宏大",
            writing_style="简洁",
        )
        assert ctx.trace_id == "t1"
        assert ctx.writing_style == "简洁"


# ── WriterHarness 抽象 ──────────────────────────────────────


class TestWriterHarnessAbstract:
    def test_cannot_instantiate_directly(self) -> None:
        """WriterHarness 是 ABC，不能直接实例化。"""
        with pytest.raises(TypeError):
            WriterHarness()  # type: ignore[abstract]

    def test_incomplete_subclass_fails(self) -> None:
        """子类缺方法不能实例化。"""
        class Incomplete(WriterHarness):
            def build_system_prompt(self, ctx):
                return ""
        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_complete_subclass_works(self) -> None:
        """实现所有抽象方法的子类可实例化。"""
        class Complete(WriterHarness):
            def build_system_prompt(self, ctx):
                return "prompt"
            def build_skills(self, ctx):
                return ["/skills/a"]
            def build_middleware(self, ctx):
                return []
            def build_subagents(self, ctx):
                return []
        h = Complete()
        ctx = HarnessContext(workspace_path=Path("/tmp"))
        assert h.build_system_prompt(ctx) == "prompt"
        assert h.build_skills(ctx) == ["/skills/a"]
        assert h.build_tools(ctx) == []  # 默认空
        assert h.harness_id() == "Complete"  # 默认类名


# ── SubagentHarness ─────────────────────────────────────────


class _NormalSubagentHarness(SubagentHarness):
    """普通 subagent harness（is_deep=False）。"""
    @property
    def name(self) -> str:
        return "interview"
    def build_description(self, ctx):
        return "desc"
    def build_system_prompt(self, ctx):
        return "prompt"
    def build_middleware(self, ctx):
        return []
    def build_permissions(self, ctx):
        return []


class _DeepSubagentHarness(SubagentHarness):
    """Deep subagent harness（is_deep=True）。"""
    @property
    def name(self) -> str:
        return "writing"
    @property
    def is_deep(self) -> bool:
        return True
    def build_description(self, ctx):
        return "desc"
    def build_system_prompt(self, ctx):
        return "prompt"
    def build_middleware(self, ctx):
        return []
    def build_permissions(self, ctx):
        return []
    def build_deep_params(self, ctx):
        return {"system_prompt": "prompt", "max_revisions": 1}


class TestSubagentHarness:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            SubagentHarness()  # type: ignore[abstract]

    def test_is_deep_default_false(self) -> None:
        h = _NormalSubagentHarness()
        assert h.is_deep is False

    def test_build_spec_default_assembly(self) -> None:
        """普通 subagent 的 build_spec 默认组装 name/desc/prompt/permissions/middleware。"""
        h = _NormalSubagentHarness()
        ctx = HarnessContext(workspace_path=Path("/tmp"))
        spec = h.build_spec(ctx)
        assert spec["name"] == "interview"
        assert spec["description"] == "desc"
        assert spec["system_prompt"] == "prompt"
        assert spec["permissions"] == []
        assert spec["middleware"] == []

    def test_build_skills_default_empty(self) -> None:
        h = _NormalSubagentHarness()
        assert h.build_skills(HarnessContext(workspace_path=Path("/tmp"))) == []

    def test_deep_subagent_is_deep_true(self) -> None:
        h = _DeepSubagentHarness()
        assert h.is_deep is True

    def test_deep_subagent_build_deep_params(self) -> None:
        h = _DeepSubagentHarness()
        ctx = HarnessContext(workspace_path=Path("/tmp"))
        params = h.build_deep_params(ctx)
        assert params["system_prompt"] == "prompt"
        assert params["max_revisions"] == 1

    def test_normal_subagent_build_deep_params_raises(self) -> None:
        """普通 subagent 调 build_deep_params 应抛 NotImplementedError。"""
        h = _NormalSubagentHarness()
        ctx = HarnessContext(workspace_path=Path("/tmp"))
        with pytest.raises(NotImplementedError):
            h.build_deep_params(ctx)


# ── harness_id ─────────────────────────────────────────────


class TestHarnessId:
    def test_default_is_class_name(self) -> None:
        class MyHarness(WriterHarness):
            def build_system_prompt(self, ctx): return ""
            def build_skills(self, ctx): return []
            def build_middleware(self, ctx): return []
            def build_subagents(self, ctx): return []
        assert MyHarness().harness_id() == "MyHarness"
