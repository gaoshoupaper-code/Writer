"""Phase 6 迁移脚本生成首版 manifest 验证（T5.1 端到端数据层）。

留在 evolution 测试目录（测 evolution 的迁移脚本 + manifest_repo）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MigrationBootstrapTest(unittest.TestCase):
    """迁移脚本生成首版 production manifest 验证。"""

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
        import app.migrate_to_surface as migrate
        importlib.reload(migrate)
        self.migrate = migrate

    def tearDown(self) -> None:
        try:
            self.db.get_conn().close()
        except Exception:
            pass

    def test_migrate_generates_first_manifest(self) -> None:
        """迁移脚本生成首版 production manifest（Phase 6 启动前置）。"""
        from app.improvement import manifest_repo
        result = self.migrate.run_migration()
        self.assertEqual(result["surfaces_imported"], 30)
        self.assertEqual(result["manifest"], 1)
        prod = manifest_repo.get_production_manifest()
        self.assertIsNotNone(prod)
        entries = manifest_repo.get_entries(prod)
        self.assertEqual(len(entries["surfaces"]), 30)
        c_surfaces = entries["schema_lock"]["c_surfaces"]
        self.assertEqual(len(c_surfaces), 1)
        self.assertEqual(c_surfaces[0]["surface_name"], "GoalMiddleware")

    def test_manifest_after_migration_has_all_scopes(self) -> None:
        """迁移后 manifest 覆盖所有 scope（装配完整性）。"""
        from app.improvement import manifest_repo
        self.migrate.run_migration()
        prod = manifest_repo.get_production_manifest()
        entries = manifest_repo.get_entries(prod)
        scopes = {s["scope"] for s in entries["surfaces"]}
        for expected in ("meta", "writing", "storybuilding", "detail-outline", "interview"):
            self.assertIn(expected, scopes, f"scope {expected} 缺失")

    def test_manifest_surfaces_have_content_refs(self) -> None:
        """manifest 的每个 surface entry 有 version 指针（执行端据此拉 content）。"""
        from app.improvement import manifest_repo
        self.migrate.run_migration()
        prod = manifest_repo.get_production_manifest()
        entries = manifest_repo.get_entries(prod)
        for s in entries["surfaces"]:
            self.assertIn("version", s)
            self.assertGreater(s["version"], 0)
            self.assertIn("surface_type", s)
            self.assertIn("surface_name", s)
            self.assertIn("scope", s)

    def test_idempotent_bootstrap(self) -> None:
        """重复迁移（重建基准）不破坏 manifest 一致性。"""
        from app.improvement import manifest_repo
        self.migrate.run_migration()
        v1 = manifest_repo.get_production_manifest()["manifest_version"]
        self.migrate.run_migration()
        v2 = manifest_repo.get_production_manifest()["manifest_version"]
        # 重建后 manifest 版本号可同可不同，但 surface 数必须一致
        entries2 = manifest_repo.get_entries(manifest_repo.get_production_manifest())
        self.assertEqual(len(entries2["surfaces"]), 30)


if __name__ == "__main__":
    unittest.main()
