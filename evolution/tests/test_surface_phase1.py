"""Phase 6 数据层（surface_versions + harness_manifests）测试。

覆盖：
- 新表建出 + 幂等迁移
- contracts.surface_types：类型/层/content_kind/scope 校验
- surface_repo：版本单调递增、status 流转、scope/type 查询、approved 聚合
- manifest_repo：发布聚合（D7）、production 唯一性、schema_lock、回放契约校验（D12）
- 全局锁快照幂等（多次发布不漂移）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SurfacePhase1Test(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        os.environ["EVOLUTION_DB"] = str(Path(self._tmpdir) / "test.db")
        os.environ["EXECUTOR_WORKSPACE"] = self._tmpdir
        import importlib
        import app.core.settings as settings_mod
        importlib.reload(settings_mod)
        import app.core.db as db
        db._conn = None
        db.init_db()
        self.db = db

    def tearDown(self) -> None:
        # 清空本阶段表，避免跨测试方法状态泄漏（同 tempdb 复用）
        try:
            conn = self.db.get_conn()
            conn.execute("DELETE FROM surface_versions")
            conn.execute("DELETE FROM harness_manifests")
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ── 表结构 ──────────────────────────────────────────────

    def test_new_tables_exist(self) -> None:
        conn = self.db.get_conn()
        for t in ("surface_versions", "harness_manifests"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            self.assertIsNotNone(row, f"表 {t} 未建出")

    def test_failure_signatures_has_surface_columns(self) -> None:
        conn = self.db.get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(failure_signatures)").fetchall()]
        self.assertIn("surface_type", cols)
        self.assertIn("surface_scope", cols)

    def test_init_db_idempotent(self) -> None:
        """多次 init_db 不报错（迁移幂等）。"""
        self.db.init_db()
        self.db.init_db()

    # ── surface_registry ────────────────────────────────────

    def test_registry_known_types(self) -> None:
        from contracts import surface_types as sr
        types = sr.list_types()
        for expected in ("prompt", "skill", "description", "middleware_params",
                         "permissions", "stateful_middleware"):
            self.assertIn(expected, types)

    def test_registry_layer_mapping(self) -> None:
        from contracts import surface_types as sr
        self.assertEqual(sr.get_layer("prompt"), sr.SurfaceLayer.A_TEXT)
        self.assertEqual(sr.get_layer("middleware_params"), sr.SurfaceLayer.B_PARAM)
        self.assertEqual(sr.get_layer("stateful_middleware"), sr.SurfaceLayer.C_CODE)
        self.assertTrue(sr.is_c_code("stateful_middleware"))
        self.assertFalse(sr.is_c_code("prompt"))

    def test_registry_rejects_unknown(self) -> None:
        from contracts import surface_types as sr
        with self.assertRaises(KeyError):
            sr.get_type_def("nonexistent_type")
        with self.assertRaises(ValueError):
            sr.validate_scope("bad_scope")

    def test_registry_content_kind_consistent(self) -> None:
        """content_kind 由 surface_type 决定（编译期一致性）。"""
        from contracts import surface_types as sr
        self.assertEqual(sr.get_content_kind("prompt"), sr.ContentKind.TEXT)
        self.assertEqual(sr.get_content_kind("middleware_params"), sr.ContentKind.JSON)
        self.assertEqual(sr.get_content_kind("stateful_middleware"), sr.ContentKind.PYTHON)

    # ── surface_repo：版本管理 ──────────────────────────────

    def test_create_version_monotonic(self) -> None:
        from app.improvement import surface_repo
        v1 = surface_repo.create_version("prompt", "writing_system", "writing", "v1 content")
        v2 = surface_repo.create_version("prompt", "writing_system", "writing", "v2 content")
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v2["version"], 2)

    def test_create_version_independent_lines(self) -> None:
        """不同 surface 线 version 独立递增。"""
        from app.improvement import surface_repo
        a1 = surface_repo.create_version("prompt", "writing_system", "writing", "a1")
        b1 = surface_repo.create_version("prompt", "storybuilding_system", "storybuilding", "b1")
        a2 = surface_repo.create_version("prompt", "writing_system", "writing", "a2")
        self.assertEqual(a1["version"], 1)
        self.assertEqual(b1["version"], 1)  # 不同线独立
        self.assertEqual(a2["version"], 2)

    def test_create_version_content_kind_auto(self) -> None:
        """content_kind 由 surface_type 自动决定，调用方无需指定。"""
        from app.improvement import surface_repo
        v = surface_repo.create_version("stateful_middleware", "GoalMiddleware", "meta", "code")
        self.assertEqual(v["content_kind"], "python")

    def test_create_version_rejects_unknown_type(self) -> None:
        from app.improvement import surface_repo
        with self.assertRaises(KeyError):
            surface_repo.create_version("bad_type", "x", "writing", "c")

    def test_create_version_rejects_bad_scope(self) -> None:
        from app.improvement import surface_repo
        with self.assertRaises(ValueError):
            surface_repo.create_version("prompt", "x", "bad_scope", "c")

    def test_create_version_rejects_bad_status(self) -> None:
        from app.improvement import surface_repo
        with self.assertRaises(ValueError):
            surface_repo.create_version("prompt", "x", "writing", "c", status="bad")

    # ── surface_repo：查询 ──────────────────────────────────

    def test_list_versions_with_status_filter(self) -> None:
        from app.improvement import surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "c1", status="approved")
        surface_repo.create_version("prompt", "p1", "writing", "c2", status="draft")
        approved = surface_repo.list_versions("prompt", "p1", "writing", status="approved")
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["version"], 1)

    def test_get_approved_version(self) -> None:
        from app.improvement import surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "c1", status="approved")
        surface_repo.create_version("prompt", "p1", "writing", "c2", status="approved")
        # approved 最高版 = v2
        av = surface_repo.get_approved_version("prompt", "p1", "writing")
        self.assertIsNotNone(av)
        self.assertEqual(av["version"], 2)

    def test_get_approved_version_none(self) -> None:
        from app.improvement import surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "c1", status="draft")
        self.assertIsNone(surface_repo.get_approved_version("prompt", "p1", "writing"))

    def test_same_name_different_scope_independent(self) -> None:
        """同名 surface 在不同 scope 是独立的线（UNIQUE 含 scope 的核心价值）。"""
        from app.improvement import surface_repo
        # ContextAssembler 在 storybuilding 和 writing 用不同参数——必须独立版本线
        a = surface_repo.create_version(
            "middleware_params", "ContextAssembler", "storybuilding",
            '{"args":{"file_paths":["demand.md"]}}', status="approved",
        )
        b = surface_repo.create_version(
            "middleware_params", "ContextAssembler", "writing",
            '{"args":{"file_paths":["demand.md","detail/*.md"]}}', status="approved",
        )
        self.assertEqual(a["version"], 1)  # 各自独立从 1 开始
        self.assertEqual(b["version"], 1)
        # 按 scope 各取各的
        sa = surface_repo.get_approved_version("middleware_params", "ContextAssembler", "storybuilding")
        sw = surface_repo.get_approved_version("middleware_params", "ContextAssembler", "writing")
        self.assertIn("demand.md", sa["content"])
        self.assertIn("detail/*.md", sw["content"])

    def test_list_by_scope(self) -> None:
        from app.improvement import surface_repo
        surface_repo.create_version("prompt", "w1", "writing", "c", status="approved")
        surface_repo.create_version("prompt", "s1", "storybuilding", "c", status="approved")
        writing_only = surface_repo.list_by_scope("writing", status="approved")
        self.assertEqual(len(writing_only), 1)
        self.assertEqual(writing_only[0]["surface_name"], "w1")

    # ── surface_repo：status 流转 ───────────────────────────

    def test_update_status_flow(self) -> None:
        from app.improvement import surface_repo
        v = surface_repo.create_version("prompt", "p1", "writing", "c")
        surface_repo.update_status(v["id"], "static_checked", static_check_passed=True)
        self.assertEqual(surface_repo.get_version_by_id(v["id"])["status"], "static_checked")
        self.assertEqual(surface_repo.get_version_by_id(v["id"])["static_check_passed"], 1)

    def test_approve_and_reject(self) -> None:
        from app.improvement import surface_repo
        v = surface_repo.create_version("prompt", "p1", "writing", "c")
        surface_repo.approve(v["id"])
        self.assertEqual(surface_repo.get_version_by_id(v["id"])["status"], "approved")
        surface_repo.reject(v["id"])
        self.assertEqual(surface_repo.get_version_by_id(v["id"])["status"], "rejected")

    # ── manifest_repo：发布聚合 ─────────────────────────────

    def test_publish_production_empty_returns_none(self) -> None:
        """无 approved surface 时发布返回 None。"""
        from app.improvement import manifest_repo
        self.assertIsNone(manifest_repo.publish_production())

    def test_publish_production_aggregates_approved(self) -> None:
        """发布聚合所有 approved surface 版本。"""
        from app.improvement import manifest_repo, surface_repo
        surface_repo.create_version("prompt", "writing_system", "writing", "w1", status="approved")
        surface_repo.create_version("prompt", "storybuilding_system", "storybuilding", "s1", status="approved")
        # 未 approved 的不进 manifest
        surface_repo.create_version("prompt", "draft_p", "writing", "d", status="draft")

        m = manifest_repo.publish_production()
        self.assertIsNotNone(m)
        self.assertEqual(m["status"], "production")
        entries = manifest_repo.get_entries(m)
        names = {s["surface_name"] for s in entries["surfaces"]}
        self.assertIn("writing_system", names)
        self.assertIn("storybuilding_system", names)
        self.assertNotIn("draft_p", names)  # draft 不进

    def test_publish_takes_highest_approved_version(self) -> None:
        """同一线多个 approved 版本，取最高。"""
        from app.improvement import manifest_repo, surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "v1", status="approved")
        surface_repo.create_version("prompt", "p1", "writing", "v2", status="approved")
        m = manifest_repo.publish_production()
        entries = manifest_repo.get_entries(m)
        p1_entry = [s for s in entries["surfaces"] if s["surface_name"] == "p1"][0]
        self.assertEqual(p1_entry["version"], 2)

    def test_production_manifest_unique(self) -> None:
        """同时刻只有一个 production（发布新版本时旧版降 retired）。"""
        from app.improvement import manifest_repo, surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "v1", status="approved")
        m1 = manifest_repo.publish_production()
        self.assertEqual(m1["manifest_version"], 1)

        # 新增 approved surface 后再发布
        surface_repo.create_version("prompt", "p2", "writing", "v1", status="approved")
        m2 = manifest_repo.publish_production()
        self.assertEqual(m2["manifest_version"], 2)

        # m1 应已 retired
        m1_after = manifest_repo.get_manifest(1)
        self.assertEqual(m1_after["status"], "retired")
        # 只有 m2 是 production
        prod = manifest_repo.get_production_manifest()
        self.assertEqual(prod["manifest_version"], 2)

    def test_manifest_parent_and_diff(self) -> None:
        """新 manifest 记录 parent + 自动算 change_summary。"""
        from app.improvement import manifest_repo, surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "v1", status="approved")
        manifest_repo.publish_production()

        surface_repo.create_version("prompt", "p1", "writing", "v2", status="approved")
        m2 = manifest_repo.publish_production()
        self.assertEqual(m2["parent_version"], 1)
        # change_summary 应体现 p1 版本变化
        self.assertIn("p1", m2["change_summary"])
        self.assertIn("v1", m2["change_summary"])
        self.assertIn("v2", m2["change_summary"])

    # ── manifest_repo：schema_lock ──────────────────────────

    def test_schema_lock_captures_c_surfaces(self) -> None:
        """C 类 surface 进 schema_lock（回放契约），A/B 类不进。"""
        from app.improvement import manifest_repo, surface_repo
        surface_repo.create_version("prompt", "p1", "writing", "text", status="approved")  # A 类
        surface_repo.create_version("stateful_middleware", "GoalMiddleware", "meta",
                                    "class GM: state_schema=X", status="approved")  # C 类
        m = manifest_repo.publish_production()
        entries = manifest_repo.get_entries(m)
        c_names = {s["surface_name"] for s in entries["schema_lock"]["c_surfaces"]}
        self.assertIn("GoalMiddleware", c_names)
        self.assertNotIn("p1", c_names)  # A 类不进 schema_lock

    # ── manifest_repo：回放契约校验 ─────────────────────────

    def test_replay_compatible_match(self) -> None:
        """回放 manifest 的 C 类版本与 trace 一致 → 兼容。"""
        from app.improvement import manifest_repo, surface_repo
        surface_repo.create_version("stateful_middleware", "GoalMiddleware", "meta",
                                    "code", status="approved")
        m = manifest_repo.publish_production()
        ok, mismatches = manifest_repo.check_replay_compatible(
            m, [{"surface_name": "GoalMiddleware", "scope": "meta", "version": 1}]
        )
        self.assertTrue(ok)
        self.assertEqual(mismatches, [])

    def test_replay_incompatible_version_mismatch(self) -> None:
        """C 类版本不一致 → 不兼容（回放失真拦截）。"""
        from app.improvement import manifest_repo, surface_repo
        # 造 v1 + v2 两个 approved 版本，manifest 取最高 v2
        surface_repo.create_version("stateful_middleware", "GoalMiddleware", "meta",
                                    "code v1", status="approved")
        surface_repo.create_version("stateful_middleware", "GoalMiddleware", "meta",
                                    "code v2", status="approved")
        m = manifest_repo.publish_production()
        # trace 记录的是 v1，但当前 manifest 是 v2 → 不兼容
        ok, mismatches = manifest_repo.check_replay_compatible(
            m, [{"surface_name": "GoalMiddleware", "scope": "meta", "version": 1}]
        )
        self.assertFalse(ok)
        self.assertTrue(any("版本不一致" in msg for msg in mismatches))


if __name__ == "__main__":
    unittest.main()
