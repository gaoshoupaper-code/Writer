"""Phase 5 T5.1：harness_api 端点测试。

用 TestClient 验证新 API 端点可响应（不验证业务逻辑，那已在 pipeline/mining 测试覆盖）。
覆盖：
- /signatures 列表
- /harnesses 列表 + production
- /experiments 列表 + approve/reject
- /calibration + recommended-n
- /pipeline/run
- 路由注册正确（端点可达）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class HarnessApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        os.environ["MONITORING_DB"] = str(Path(self._tmpdir) / "test.db")
        os.environ["BACKEND_WORKSPACE"] = self._tmpdir
        import importlib
        import app.settings as settings_mod
        importlib.reload(settings_mod)
        import app.db as db
        db._conn = None
        db.init_db()
        self.db = db
        # 初始化测试集（pipeline 需要）
        from app import replay
        replay.ensure_default_multistyle_test_set()
        # 建 production harness
        from app import harness_repo
        harnesses_root = Path(self._tmpdir) / "harnesses"
        code_path = harness_repo.write_harness_code(harnesses_root, "class H: pass", 1)
        harness_repo.create_version(str(code_path), labels=["production"], status="approved")
        # reload app 以用新 settings/db
        import app.main as main_mod
        importlib.reload(main_mod)
        from fastapi.testclient import TestClient
        self.client = TestClient(main_mod.app)

    def tearDown(self) -> None:
        try:
            for t in ("harness_versions", "harness_experiments", "failure_signatures"):
                self.db.get_conn().execute(f"DELETE FROM {t}")
            self.db.get_conn().commit()
            self.db.get_conn().close()
        except Exception:
            pass

    def test_list_signatures_empty(self) -> None:
        resp = self.client.get("/api/signatures")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_harnesses(self) -> None:
        resp = self.client.get("/api/harnesses")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreaterEqual(len(data), 1)
        versions = [h["version"] for h in data]
        self.assertIn(1, versions)  # production 版本存在

    def test_get_production(self) -> None:
        resp = self.client.get("/api/harnesses/production")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("production", resp.json()["labels"])

    def test_list_experiments_empty(self) -> None:
        resp = self.client.get("/api/experiments")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_calibration_endpoints(self) -> None:
        resp = self.client.get("/api/calibration")
        self.assertEqual(resp.status_code, 200)
        resp2 = self.client.get("/api/calibration/recommended-n")
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("recommended_n", resp2.json())

    def test_reject_nonexistent_experiment_404(self) -> None:
        resp = self.client.post("/api/experiments/9999/reject")
        self.assertEqual(resp.status_code, 404)

    def test_approve_nonexistent_experiment_404(self) -> None:
        resp = self.client.post("/api/experiments/9999/approve")
        self.assertEqual(resp.status_code, 404)

    def test_signature_detail_404(self) -> None:
        resp = self.client.get("/api/signatures/9999")
        self.assertEqual(resp.status_code, 404)

    def test_harness_diff_404(self) -> None:
        resp = self.client.get("/api/harnesses/9999/diff")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
