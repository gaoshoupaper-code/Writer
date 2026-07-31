"""Phase 4 prompt 版本管理测试（T9）。

覆盖：版本递增、label 互斥、按 label 拉取、幂等导入。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class PromptsRepoTest(unittest.TestCase):
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
        try:
            self.db.get_conn().close()
        except Exception:
            pass

    def test_version_increments(self) -> None:
        import app.improvement.prompts_repo as repo
        prompt = repo.create_prompt("test1")
        v1 = repo.create_version(prompt["id"], "content v1")
        v2 = repo.create_version(prompt["id"], "content v2")
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v2["version"], 2)

    def test_label_exclusive(self) -> None:
        """同 prompt 下 production label 同时只指向一个版本。"""
        import app.improvement.prompts_repo as repo
        prompt = repo.create_prompt("test2")
        v1 = repo.create_version(prompt["id"], "c1", labels=["production"])
        v2 = repo.create_version(prompt["id"], "c2", labels=["production"])
        # v1 的 production 应被移除（互斥）
        v1_after = repo.get_version(prompt["id"], 1)
        self.assertNotIn("production", v1_after["labels"])
        self.assertIn("production", v2["labels"])

    def test_new_version_gets_latest(self) -> None:
        """新版本默认打 latest label。"""
        import app.improvement.prompts_repo as repo
        prompt = repo.create_prompt("test3")
        v1 = repo.create_version(prompt["id"], "c1")
        self.assertIn("latest", v1["labels"])
        v2 = repo.create_version(prompt["id"], "c2")
        # v2 成为 latest，v1 失去 latest
        v1_after = repo.get_version(prompt["id"], 1)
        self.assertNotIn("latest", v1_after["labels"])
        self.assertIn("latest", v2["labels"])

    def test_get_by_label(self) -> None:
        """按 label 拉取（后端 loader 主入口）。"""
        import app.improvement.prompts_repo as repo
        prompt = repo.create_prompt("test4")
        repo.create_version(prompt["id"], "old", labels=["production"])
        repo.create_version(prompt["id"], "new", labels=["production"])
        content = repo.get_prompt_content("test4", "production")
        self.assertIsNotNone(content)
        self.assertEqual(content["content"], "new")  # production 指向最新
        self.assertEqual(content["version"], 2)

    def test_get_fallback_to_latest(self) -> None:
        """label 未找到时回退到最新版本。"""
        import app.improvement.prompts_repo as repo
        prompt = repo.create_prompt("test5")
        repo.create_version(prompt["id"], "only")  # 无 production label
        content = repo.get_prompt_content("test5", "production")
        self.assertIsNotNone(content)
        self.assertEqual(content["content"], "only")

    def test_idempotent_import(self) -> None:
        """导入脚本是幂等的（重复导入跳过已存在）。"""
        import app.improvement.prompt_import as pi
        backend_root = Path(self._tmpdir) / "backend"
        prompts_dir = backend_root / "app" / "domains" / "writing" / "meta" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "system.md").write_text("test prompt", encoding="utf-8")

        r1 = pi.import_backend_prompts(backend_root)
        r2 = pi.import_backend_prompts(backend_root)
        self.assertGreaterEqual(r1["imported"], 1)
        self.assertEqual(r2["imported"], 0)  # 幂等：第二次全跳过


if __name__ == "__main__":
    unittest.main()
