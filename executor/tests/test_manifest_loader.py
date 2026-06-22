"""manifest_loader 测试（Phase 6 T4.x）。

验证 assemble 把 manifest JSON 正确解析成 AssembledManifest（装配意图），
且装配输出等价于 v1 harness 的装配（不含 model/backend，那些归执行端）。

不依赖 evolution HTTP（直接构造 manifest dict 喂 assemble）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.platform.harness.manifest_loader import (
    AssembledManifest,
    AssembledSubagent,
    CSurfaceLoadError,
    assemble,
    preload_c_surfaces,
)


def _make_surface(
    surface_type: str, surface_name: str, scope: str, version: int,
    content: str, config: dict | None = None,
) -> dict:
    """构造一个 manifest surface entry（含 _content，模拟 enrich 后）。"""
    return {
        "surface_type": surface_type, "surface_name": surface_name,
        "scope": scope, "version": version, "id": 1,
        "_content": content, "_config": config or {},
    }


def _make_manifest(surfaces: list[dict], c_surfaces: list[dict] | None = None) -> dict:
    """构造完整 manifest（已 enrich，_content 已填）。"""
    return {
        "manifest_version": 1,
        "entries": {
            "surfaces": surfaces,
            "schema_lock": {"c_surfaces": c_surfaces or []},
        },
    }


# 一份覆盖所有 surface 类型的 manifest（模拟迁移脚本产出）
def _full_manifest() -> dict:
    surfaces = [
        # A 类 prompt（8 个，每 scope 至少 1）
        _make_surface("prompt", "meta_system", "meta", 1, "你是总指挥"),
        _make_surface("prompt", "interview_system", "interview", 1, "你是访谈员"),
        _make_surface("prompt", "storybuilding_system", "storybuilding", 1, "你是故事建筑师"),
        _make_surface("prompt", "storybuilding_evaluation", "storybuilding", 1, "评估故事"),
        _make_surface("prompt", "detail_outline_system", "detail-outline", 1, "你是分场细化员"),
        _make_surface("prompt", "writing_system", "writing", 1, "你是正文写手"),
        # A 类 skill（含 rel_dir config）
        _make_surface("skill", "auto-pipeline", "meta", 1, "SKILL 内容",
                      {"rel_dir": "domains/writing/meta/skills/auto-pipeline"}),
        _make_surface("skill", "chapter-writing", "writing", 1, "SKILL 内容",
                      {"rel_dir": "domains/writing/expert_agent/skills/writing/chapter-writing"}),
        # A 类 description
        _make_surface("description", "description/writing", "writing", 1, "适用：写正文"),
        _make_surface("description", "description/interview", "interview", 1, "适用：访谈"),
        _make_surface("description", "description/storybuilding", "storybuilding", 1, "适用：搭故事"),
        # B 类 middleware_params（含 ${ctx} 占位符）
        _make_surface("middleware_params", "ContextAssembler", "writing", 1,
                      json.dumps({"class": "ContextAssemblerMiddleware",
                                  "args": {"workspace_root": "${ctx.workspace_path}",
                                           "file_paths": ["demand.md", "detail/*.md"]}})),
        # B 类 permissions
        _make_surface("permissions", "permissions/writing", "writing", 1,
                      json.dumps([{"operations": ["read"], "paths": ["/**"], "mode": "allow"}])),
        # B 类 deep_meta
        _make_surface("middleware_params", "deep_meta/writing", "writing", 1,
                      json.dumps({"evaluator_kind": "writing", "max_revisions": 1,
                                  "artifact_paths": []})),
        _make_surface("middleware_params", "deep_meta/storybuilding", "storybuilding", 1,
                      json.dumps({"evaluator_kind": "storybuilding", "max_revisions": 1,
                                  "artifact_paths": ["storyline.md"]})),
    ]
    c_surfaces = [{"surface_name": "GoalMiddleware", "scope": "meta", "version": 1}]
    return _make_manifest(surfaces, c_surfaces)


# ── assemble 基础测试 ────────────────────────────────────────


class TestAssemble:
    def test_assemble_returns_assembled_manifest(self) -> None:
        result = assemble(_full_manifest())
        assert isinstance(result, AssembledManifest)
        assert result.manifest_version == 1

    def test_meta_system_prompt_extracted(self) -> None:
        result = assemble(_full_manifest())
        assert result.meta_system_prompt == "你是总指挥"
        assert result.meta_prompt_version == 1

    def test_meta_skills_resolved_to_absolute(self) -> None:
        result = assemble(_full_manifest())
        # meta skills 只取 meta scope（auto-pipeline），不含 subagent skill
        assert len(result.meta_skills) == 1
        # 路径用 Path 比较（跨平台分隔符）
        skill_path = Path(result.meta_skills[0])
        assert skill_path.is_absolute()
        assert skill_path.parts[-4:] == ("writing", "meta", "skills", "auto-pipeline")

    def test_subagents_count_5(self) -> None:
        """5 个 subagent：general-purpose + interview + 3 deep。"""
        result = assemble(_full_manifest())
        assert len(result.subagents) == 5
        names = {s.name for s in result.subagents}
        assert names == {"general-purpose", "interview", "storybuilding",
                         "detail-outline", "writing"}

    def test_general_purpose_kind(self) -> None:
        result = assemble(_full_manifest())
        gp = next(s for s in result.subagents if s.name == "general-purpose")
        assert gp.kind == "general_purpose"

    def test_interview_is_custom_kind(self) -> None:
        result = assemble(_full_manifest())
        interview = next(s for s in result.subagents if s.name == "interview")
        assert interview.kind == "custom"
        assert interview.system_prompt == "你是访谈员"
        assert interview.description == "适用：访谈"

    def test_deep_subagent_has_evaluator_kind(self) -> None:
        result = assemble(_full_manifest())
        writing = next(s for s in result.subagents if s.name == "writing")
        assert writing.kind == "deep"
        assert writing.system_prompt == "你是正文写手"
        assert writing.evaluator_kind == "writing"
        assert writing.max_revisions == 1

    def test_deep_subagent_artifact_paths(self) -> None:
        result = assemble(_full_manifest())
        sb = next(s for s in result.subagents if s.name == "storybuilding")
        assert sb.evaluator_kind == "storybuilding"
        assert "storyline.md" in sb.artifact_paths

    def test_deep_subagent_middleware_specs(self) -> None:
        """deep subagent 的 subagent_middleware 是参数化规格（dict，待执行端实例化）。"""
        result = assemble(_full_manifest())
        writing = next(s for s in result.subagents if s.name == "writing")
        # ContextAssembler 规格在
        assert len(writing.subagent_middleware) == 1
        spec = writing.subagent_middleware[0]
        assert spec["class"] == "ContextAssemblerMiddleware"
        # 占位符保留（不替换为具体值）
        assert spec["args"]["workspace_root"] == "${ctx.workspace_path}"
        assert "detail/*.md" in spec["args"]["file_paths"]

    def test_manifest_meta_captured(self) -> None:
        result = assemble(_full_manifest())
        assert result.manifest_meta["manifest_version"] == 1
        assert len(result.manifest_meta["c_surfaces"]) == 1


# ── C 类预加载测试 ───────────────────────────────────────────


_VALID_C_CODE = '''
from langchain.agents.middleware.types import AgentMiddleware

class TestMiddleware(AgentMiddleware):
    state_schema = dict
    def after_model(self, state, runtime):
        return None
'''

_INVALID_C_CODE_NO_MW = "def foo(): return 1\n"

_INVALID_C_CODE_NO_SCHEMA = '''
from langchain.agents.middleware.types import AgentMiddleware
class BadMW(AgentMiddleware):
    def after_model(self, state, runtime):
        return None
'''


class TestPreloadCSurfaces:
    def test_preload_valid_c_surface(self) -> None:
        entries = {"surfaces": [
            {"surface_type": "stateful_middleware", "surface_name": "TestMW",
             "scope": "meta", "version": 1, "_content": _VALID_C_CODE},
        ]}
        pool = preload_c_surfaces(entries)
        assert ("TestMW", "meta") in pool
        assert pool[("TestMW", "meta")]["instance"] is not None
        assert pool[("TestMW", "meta")]["state_schema"] is not None

    def test_preload_collects_state_schema(self) -> None:
        """预加载后，assemble 能收集到 state_schemas。"""
        entries = {"surfaces": [
            {"surface_type": "stateful_middleware", "surface_name": "TestMW",
             "scope": "meta", "version": 1, "_content": _VALID_C_CODE},
        ]}
        preload_c_surfaces(entries)
        # 构造含该 C 类的 manifest 跑 assemble
        manifest = _make_manifest([
            _make_surface("prompt", "meta_system", "meta", 1, "prompt"),
            {"surface_type": "stateful_middleware", "surface_name": "TestMW",
             "scope": "meta", "version": 1, "_content": _VALID_C_CODE},
        ], [{"surface_name": "TestMW", "scope": "meta", "version": 1}])
        result = assemble(manifest)
        assert len(result.state_schemas) == 1
        # GoalMiddleware 在 meta_middleware_base 里（如果 C 类 scope 是 meta）
        # 但 TestMW 不是 GoalMiddleware，所以 meta_middleware_base 不含它
        # （_instantiate_meta_middleware 只取 GoalMiddleware）

    def test_preload_rejects_no_middleware_class(self) -> None:
        entries = {"surfaces": [
            {"surface_type": "stateful_middleware", "surface_name": "Bad1",
             "scope": "meta", "version": 1, "_content": _INVALID_C_CODE_NO_MW},
        ]}
        with pytest.raises(CSurfaceLoadError, match="未定义 AgentMiddleware 子类"):
            preload_c_surfaces(entries)

    def test_preload_accepts_no_explicit_state_schema(self) -> None:
        """执行端放宽：缺显式 state_schema 的 middleware 也能加载（取基类默认）。

        state_schema 契约的严格检查在 evolution static_check（源码级 AST），
        执行端只验证"能加载 + 是 middleware 子类 + 能实例化"。
        """
        entries = {"surfaces": [
            {"surface_type": "stateful_middleware", "surface_name": "Bad2",
             "scope": "meta", "version": 1, "_content": _INVALID_C_CODE_NO_SCHEMA},
        ]}
        pool = preload_c_surfaces(entries)  # 不抛异常
        assert ("Bad2", "meta") in pool
        # state_schema 是基类默认值（_DefaultAgentState）
        assert pool[("Bad2", "meta")]["state_schema"] is not None

    def test_preload_resets_cache(self) -> None:
        """重复 preload 重置缓存（重启语义）。"""
        entries1 = {"surfaces": [
            {"surface_type": "stateful_middleware", "surface_name": "MW1",
             "scope": "meta", "version": 1, "_content": _VALID_C_CODE},
        ]}
        preload_c_surfaces(entries1)
        assert ("MW1", "meta") in __import__(
            "app.platform.harness.manifest_loader", fromlist=["_c_surface_cache"]
        )._c_surface_cache
        # 第二次 preload 不同的 surface，MW1 应被清掉
        entries2 = {"surfaces": [
            {"surface_type": "stateful_middleware", "surface_name": "MW2",
             "scope": "meta", "version": 1, "_content": _VALID_C_CODE},
        ]}
        preload_c_surfaces(entries2)
        cache = __import__(
            "app.platform.harness.manifest_loader", fromlist=["_c_surface_cache"]
        )._c_surface_cache
        assert ("MW1", "meta") not in cache
        assert ("MW2", "meta") in cache


# ── 真实 GoalMiddleware 加载测试（等价性关键）────────────────


class TestRealGoalMiddleware:
    def test_real_goal_middleware_loads(self) -> None:
        """真实 GoalMiddleware 代码能被 importlib 加载（等价性核心验证）。

        这是 C 类 surface 的真实用例：迁移脚本把 goal_middleware.py 存进 surface，
        执行端 preload 时 importlib 加载它，必须能成功（依赖 app.domains.writing.tools）。
        """
        from app.platform.harness.manifest_loader import _load_c_middleware
        gm_path = (Path(__file__).resolve().parent.parent / "app" / "domains" /
                   "writing" / "middleware" / "goal_middleware.py")
        code = gm_path.read_text(encoding="utf-8")
        instance, state_schema = _load_c_middleware("GoalMiddleware", "meta", 1, code)
        assert instance is not None
        # state_schema 应是 GoalState
        assert hasattr(state_schema, "__name__")
        assert "Goal" in state_schema.__name__ or "State" in state_schema.__name__
