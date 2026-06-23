"""contracts.manifest_schema wire-format 契约测试。

验证 manifest entries_json 的结构定义：
  - SurfaceEntry 含 5 个必填字段
  - CSurfaceRef 含 3 个字段（schema_lock 用）
  - ManifestEntries 含 surfaces + schema_lock 两层
"""
from __future__ import annotations

from contracts import manifest_schema as ms


class TestSurfaceEntry:
    def test_required_fields(self) -> None:
        entry: ms.SurfaceEntry = {
            "surface_type": "prompt",
            "surface_name": "meta_system_prompt",
            "scope": "meta",
            "version": 3,
            "id": 42,
        }
        assert entry["surface_type"] == "prompt"
        assert entry["surface_name"] == "meta_system_prompt"
        assert entry["scope"] == "meta"
        assert entry["version"] == 3
        assert entry["id"] == 42


class TestCSurfaceRef:
    def test_required_fields(self) -> None:
        ref: ms.CSurfaceRef = {
            "surface_name": "GoalMiddleware",
            "scope": "meta",
            "version": 1,
        }
        assert ref["surface_name"] == "GoalMiddleware"
        assert ref["version"] == 1


class TestManifestEntries:
    def test_full_structure(self) -> None:
        """模拟 manifest_repo._build_entries 产出的完整结构。"""
        entries: ms.ManifestEntries = {
            "surfaces": [
                {
                    "surface_type": "prompt",
                    "surface_name": "meta_system_prompt",
                    "scope": "meta",
                    "version": 1,
                    "id": 1,
                },
                {
                    "surface_type": "stateful_middleware",
                    "surface_name": "GoalMiddleware",
                    "scope": "meta",
                    "version": 1,
                    "id": 2,
                },
            ],
            "schema_lock": {
                "c_surfaces": [
                    {"surface_name": "GoalMiddleware", "scope": "meta", "version": 1},
                ],
            },
        }
        assert len(entries["surfaces"]) == 2
        assert len(entries["schema_lock"]["c_surfaces"]) == 1
        # C 类 surface 进 schema_lock
        c_ref = entries["schema_lock"]["c_surfaces"][0]
        assert c_ref["surface_name"] == "GoalMiddleware"
