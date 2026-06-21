"""Phase 0 T0.2：多文风多题材测试集测试（D21 + S3）。

覆盖：
- default-multistyle 创建 + 幂等
- 4 类（爽文/文艺/慢热/现实）各 3 条 = 12 条
- style_profile 字段正确分布（误伤高发区标注）
- 与 default-xianxia 并存不冲突
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MultiStyleTestSetTest(unittest.TestCase):
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

    def tearDown(self) -> None:
        try:
            self.db.get_conn().close()
        except Exception:
            pass

    def test_multistyle_created_with_12_items(self) -> None:
        """多文风测试集应含 12 条（4 类 × 3）。"""
        from app import replay
        ts = replay.ensure_default_multistyle_test_set()
        self.assertEqual(ts["name"], "default-multistyle")
        self.assertEqual(len(ts["prompts"]), 12)

    def test_multistyle_idempotent(self) -> None:
        """重复调用 ensure 不重复创建。"""
        from app import replay
        replay.ensure_default_multistyle_test_set()
        replay.ensure_default_multistyle_test_set()
        sets = replay.list_test_sets()
        multistyle = [s for s in sets if s["name"] == "default-multistyle"]
        self.assertEqual(len(multistyle), 1)

    def test_four_styles_covered(self) -> None:
        """4 种 style_profile 各 3 条。"""
        from app import replay
        ts = replay.ensure_default_multistyle_test_set()
        profiles = Counter(p["style_profile"] for p in ts["prompts"])
        self.assertEqual(profiles["dense_payoff"], 3)   # 爽文（进化正目标）
        self.assertEqual(profiles["literary"], 3)        # 文艺（误伤高发区）
        self.assertEqual(profiles["slow_burn"], 3)       # 慢热（误伤高发区）
        self.assertEqual(profiles["realistic"], 3)       # 现实（跨题材）

    def test_literary_items_oppose_dense_payoff(self) -> None:
        """文艺向条目应明确反对爽点密集（这是误伤检测的关键信号）。"""
        from app import replay
        ts = replay.ensure_default_multistyle_test_set()
        literary = [p for p in ts["prompts"] if p["style_profile"] == "literary"]
        self.assertEqual(len(literary), 3)
        # 每条文艺向都应含『不要』或『反对』快节奏爽点的信号
        for item in literary:
            self.assertTrue(
                "不要" in item["request"] or "反对" in item["request"] or "不" in item["request"],
                f"文艺向条目缺少『反对爽点密集』的信号: {item['request'][:50]}",
            )

    def test_multistyle_coexists_with_xianxia(self) -> None:
        """多文风测试集与原 default-xianxia 并存。"""
        from app import replay
        replay.ensure_default_test_set()
        replay.ensure_default_multistyle_test_set()
        sets = replay.list_test_sets()
        names = {s["name"] for s in sets}
        self.assertIn("default-xianxia", names)
        self.assertIn("default-multistyle", names)

    def test_every_item_has_required_fields(self) -> None:
        """每条都有 request/genre/style_profile 三字段。"""
        from app import replay
        ts = replay.ensure_default_multistyle_test_set()
        for p in ts["prompts"]:
            self.assertIn("request", p)
            self.assertIn("genre", p)
            self.assertIn("style_profile", p)
            self.assertTrue(len(p["request"]) > 20, "request 过短，不像有效创作需求")


if __name__ == "__main__":
    unittest.main()
