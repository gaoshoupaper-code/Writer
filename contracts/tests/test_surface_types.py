"""contracts.surface_types 类型契约测试。

验证 surface 体系的核心不变量：
  - 三层（A/B/C）枚举值稳定
  - scope 合法集合完整
  - REGISTRY 每个 surface_type 的 layer/content_kind 对应关系正确
  - 查询 API 行为符合契约（未知 type 抛 KeyError、未知 scope 抛 ValueError）
"""
from __future__ import annotations

import pytest

from contracts import surface_types as st


class TestSurfaceLayer:
    def test_three_layers_exist(self) -> None:
        assert st.SurfaceLayer.A_TEXT == "a_text"
        assert st.SurfaceLayer.B_PARAM == "b_param"
        assert st.SurfaceLayer.C_CODE == "c_code"

    def test_content_kind_matches_layers(self) -> None:
        assert st.get_content_kind("prompt") == st.ContentKind.TEXT
        assert st.get_content_kind("middleware_params") == st.ContentKind.JSON
        assert st.get_content_kind("stateful_middleware") == st.ContentKind.PYTHON


class TestScope:
    def test_all_scopes_valid(self) -> None:
        for scope in [st.SCOPE_META, st.SCOPE_STORYBUILDING, st.SCOPE_DETAIL_OUTLINE,
                      st.SCOPE_WRITING, st.SCOPE_INTERVIEW, st.SCOPE_GLOBAL]:
            st.validate_scope(scope)  # 不抛即通过

    def test_unknown_scope_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知 scope"):
            st.validate_scope("nonexistent-scope")


class TestRegistry:
    def test_all_types_listed(self) -> None:
        types = st.list_types()
        # 6 个内置类型
        assert set(types) == {
            "prompt", "skill", "description",
            "middleware_params", "permissions",
            "stateful_middleware",
        }

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(KeyError, match="未知 surface_type"):
            st.get_type_def("nonexistent")

    def test_layer_classification(self) -> None:
        # A 类
        assert st.get_layer("prompt") == st.SurfaceLayer.A_TEXT
        assert st.get_layer("skill") == st.SurfaceLayer.A_TEXT
        assert st.get_layer("description") == st.SurfaceLayer.A_TEXT
        # B 类
        assert st.get_layer("middleware_params") == st.SurfaceLayer.B_PARAM
        assert st.get_layer("permissions") == st.SurfaceLayer.B_PARAM
        # C 类
        assert st.get_layer("stateful_middleware") == st.SurfaceLayer.C_CODE

    def test_is_c_code_only_stateful_middleware(self) -> None:
        """只有 stateful_middleware 是 C 类（唯一能改 State schema）。"""
        assert st.is_c_code("stateful_middleware") is True
        assert st.is_c_code("prompt") is False
        assert st.is_c_code("middleware_params") is False

    def test_type_def_has_no_validator_field(self) -> None:
        """SurfaceTypeDef 不含 validator（validator 留 evolution static_check）。"""
        td = st.get_type_def("prompt")
        assert hasattr(td, "surface_type")
        assert hasattr(td, "layer")
        assert hasattr(td, "content_kind")
        assert hasattr(td, "description")
        # 关键：无 validator 字段（抽取时已剥离）
        assert not hasattr(td, "validator")

    def test_registry_is_frozen_structure(self) -> None:
        """REGISTRY 每个 entry 的 surface_type 与 key 一致。"""
        for key, td in st.REGISTRY.items():
            assert td.surface_type == key
