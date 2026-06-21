"""Phase 0 T0.1：judge 方差校准测试。

覆盖：
- judge_calibration 表迁移幂等
- recommend_seed_count 公式正确性 + 兜底
- calibrate_dimension 聚合逻辑（mock LLM）
- get_recommended_n 回退
"""
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class CalibrateTest(unittest.TestCase):
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
            # 清空校准表，避免跨测试方法状态泄漏（同 tempdb 连接复用）
            self.db.get_conn().execute("DELETE FROM judge_calibration")
            self.db.get_conn().commit()
            self.db.get_conn().close()
        except Exception:
            pass

    def test_judge_calibration_table_exists(self) -> None:
        """judge_calibration 表应在 init_db 后存在（幂等）。"""
        conn = self.db.get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='judge_calibration'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        # 再调 init_db 不报错（幂等）
        self.db.init_db()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='judge_calibration'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_recommend_seed_count_formula(self) -> None:
        """recommend_seed_count：σ=0 → 最小 N；σ 大 → N 大；有上限。"""
        from app import calibrate
        # σ=0 → 最小值
        self.assertEqual(calibrate.recommend_seed_count(0.0), calibrate._N_MIN)
        # σ=0.05 → 小 N（约 4，兜底到 5）
        self.assertGreaterEqual(calibrate.recommend_seed_count(0.05), calibrate._N_MIN)
        # σ 大 → 受 _N_MAX 封顶
        self.assertEqual(calibrate.recommend_seed_count(2.0), calibrate._N_MAX)
        # 单调性：σ 越大 N 越大（在未触顶区间）
        n_small = calibrate.recommend_seed_count(0.1)
        n_large = calibrate.recommend_seed_count(0.3)
        self.assertGreater(n_large, n_small)

    def test_recommend_seed_count_value(self) -> None:
        """验证一个具体值：σ=0.1 的 N 应符合公式。"""
        from app import calibrate
        expected = math.ceil(2 * ((1.96 + 0.84) * 0.1 / 0.1) ** 2)
        expected = max(calibrate._N_MIN, min(calibrate._N_MAX, expected))
        self.assertEqual(calibrate.recommend_seed_count(0.1), expected)
        # σ=0.1 → 约 16
        self.assertEqual(expected, 16)

    def test_get_recommended_n_fallback(self) -> None:
        """get_recommended_n：未校准维度回退默认 10。"""
        from app import calibrate
        self.assertEqual(calibrate.get_recommended_n("content", "novel", "爽点密度"), 10)

    def test_calibrate_dimension_aggregation(self) -> None:
        """calibrate_dimension：mock LLM 返回固定分数，验证聚合。"""
        from app import calibrate

        # mock llm.chat 返回固定 JSON（分数全 0.7）
        fake_raw = '{"scores": {"测试维度": 0.7}, "overall": 0.7, "verdict": "pass", "evidence": "ok"}'
        with patch("app.llm.chat", return_value=fake_raw):
            result = calibrate.calibrate_dimension(
                messages=[{"role": "user", "content": "test"}],
                metric_key="测试维度",
                layer="content",
                target="novel",
                sample_count=5,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_count"], 5)
        self.assertAlmostEqual(result["mean"], 0.7)
        # 全相同 → σ=0 → N 回退最小
        self.assertAlmostEqual(result["std"], 0.0)
        self.assertEqual(result["recommended_n"], calibrate._N_MIN)

    def test_save_and_read_calibration(self) -> None:
        """save_calibration + get_recommended_n 往返。"""
        from app import calibrate
        # 确认表初始为空（隔离前序测试）
        self.assertEqual(len(calibrate.list_calibrations()), 0)
        result = {
            "layer": "content", "target": "novel", "metric": "节奏控制",
            "sample_count": 20, "scores": [0.5, 0.6, 0.7, 0.6, 0.5],
            "mean": 0.58, "std": 0.08, "recommended_n": 13,
        }
        calibrate.save_calibration(result)
        # 读回
        n = calibrate.get_recommended_n("content", "novel", "节奏控制")
        self.assertEqual(n, 13)
        # 列表
        calibs = calibrate.list_calibrations()
        self.assertEqual(len(calibs), 1)
        self.assertEqual(calibs[0]["metric"], "节奏控制")

    def test_get_max_n_for_experiment(self) -> None:
        """get_max_n_for_experiment：取所有维度推荐 N 的最大值。"""
        from app import calibrate
        calibrate.save_calibration({
            "layer": "content", "target": "novel", "metric": "m1",
            "sample_count": 20, "scores": [], "mean": 0.5, "std": 0.1, "recommended_n": 16,
        })
        calibrate.save_calibration({
            "layer": "subagent", "target": "writing", "metric": "k1",
            "sample_count": 20, "scores": [], "mean": 0.5, "std": 0.2, "recommended_n": 30,
        })
        self.assertEqual(calibrate.get_max_n_for_experiment(), 30)


if __name__ == "__main__":
    unittest.main()
