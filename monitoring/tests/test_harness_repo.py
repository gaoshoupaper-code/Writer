"""Phase 2 T2.4：harness 版本管理测试。

覆盖：
- harness_versions 表迁移幂等
- create_version：version 单调递增 + label 互斥
- set_labels / promote_to_production：label 翻转 + 降级
- 代码文件读写 + diff
- get_version_by_label：按 label 拉取
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class HarnessRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        os.environ["MONITORING_DB"] = str(Path(self._tmpdir) / "test.db")
        os.environ["BACKEND_WORKSPACE"] = self._tmpdir
        self._harnesses_root = Path(self._tmpdir) / "harnesses"
        import importlib
        import app.settings as settings_mod
        importlib.reload(settings_mod)
        import app.db as db
        db._conn = None
        db.init_db()
        self.db = db

    def tearDown(self) -> None:
        try:
            # 清空 harness_versions 避免跨测试方法状态泄漏（同 tempdb 复用）
            self.db.get_conn().execute("DELETE FROM harness_versions")
            self.db.get_conn().commit()
            self.db.get_conn().close()
        except Exception:
            pass

    def test_harness_versions_table_exists(self) -> None:
        conn = self.db.get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='harness_versions'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        # 幂等
        self.db.init_db()

    def test_create_version_monotonic(self) -> None:
        from app import harness_repo
        v1 = harness_repo.create_version("/p/v1", labels=["production"])
        v2 = harness_repo.create_version("/p/v2", labels=["candidate"])
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v2["version"], 2)

    def test_label_mutex_on_create(self) -> None:
        """create_version 带 production label 时，旧 production 被移除。"""
        from app import harness_repo
        v1 = harness_repo.create_version("/p/v1", labels=["production"])
        v2 = harness_repo.create_version("/p/v2", labels=["production"])
        v1_after = harness_repo.get_version(1)
        self.assertNotIn("production", v1_after["labels"])
        self.assertIn("production", v2["labels"])

    def test_set_labels(self) -> None:
        from app import harness_repo
        v1 = harness_repo.create_version("/p/v1", labels=["production"])
        v2 = harness_repo.create_version("/p/v2", labels=["candidate"])
        # 把 v2 升 production
        harness_repo.set_labels(v2["id"], ["production", "latest"])
        v1_after = harness_repo.get_version(1)
        self.assertNotIn("production", v1_after["labels"])  # v1 被降级

    def test_get_version_by_label(self) -> None:
        from app import harness_repo
        harness_repo.create_version("/p/v1", labels=["production"])
        v2 = harness_repo.create_version("/p/v2", labels=["candidate"])
        prod = harness_repo.get_version_by_label("production")
        cand = harness_repo.get_version_by_label("candidate")
        self.assertEqual(prod["version"], 1)
        self.assertEqual(cand["version"], 2)

    def test_promote_to_production(self) -> None:
        """批准上线：candidate 升 production，原 production 降级。"""
        from app import harness_repo
        v1 = harness_repo.create_version("/p/v1", labels=["production"])
        v2 = harness_repo.create_version("/p/v2", labels=["candidate"], status="ab_testing")
        harness_repo.promote_to_production(v2["id"])
        prod = harness_repo.get_production_version()
        self.assertEqual(prod["version"], 2)
        self.assertEqual(prod["status"], "approved")
        v1_after = harness_repo.get_version(1)
        self.assertNotIn("production", v1_after["labels"])

    def test_update_status(self) -> None:
        from app import harness_repo
        v = harness_repo.create_version("/p/v1")
        harness_repo.update_status(v["id"], "sandbox_validating")
        self.assertEqual(harness_repo.get_version(1)["status"], "sandbox_validating")

    def test_write_and_read_harness_code(self) -> None:
        from app import harness_repo
        code = "class H(WriterHarness): pass\n"
        path = harness_repo.write_harness_code(self._harnesses_root, code, 42)
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "harness.py")
        read_back = harness_repo.read_harness_code(path)
        self.assertEqual(read_back, code)

    def test_get_harness_diff(self) -> None:
        from app import harness_repo
        code_a = "line1\nline2\nline3\n"
        code_b = "line1\nCHANGED\nline3\n"
        path_a = harness_repo.write_harness_code(self._harnesses_root, code_a, 1)
        path_b = harness_repo.write_harness_code(self._harnesses_root, code_b, 2)
        harness_repo.create_version(str(path_a), labels=["production"])
        harness_repo.create_version(str(path_b), labels=["candidate"])
        diff = harness_repo.get_harness_diff(1, 2, self._harnesses_root)
        self.assertIn("CHANGED", diff)
        self.assertIn("---", diff)  # unified diff marker

    def test_get_production_version_none_when_empty(self) -> None:
        from app import harness_repo
        self.assertIsNone(harness_repo.get_production_version())


if __name__ == "__main__":
    unittest.main()
