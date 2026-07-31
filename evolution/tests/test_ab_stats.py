"""Phase 4 T4.4：A/B 统计测试（D6 + S11 完整统计量）。

覆盖：
- mean / std 基础计算
- confidence_interval
- two_sample_t_test：win/lose/tie 判定
  - 候选显著更优（CI 下界 > prod 均值）→ win
  - 候选显著更差 → lose
  - 重叠 → tie
- 两组都无方差的退化情况
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.improvement import ab_stats


class ABStatsTest(unittest.TestCase):
    def test_mean(self) -> None:
        self.assertAlmostEqual(ab_stats.mean([1, 2, 3]), 2.0)
        self.assertEqual(ab_stats.mean([]), 0.0)

    def test_std(self) -> None:
        self.assertAlmostEqual(ab_stats.std([1, 1, 1]), 0.0)
        # [1,2,3] 的样本标准差 = 1.0
        self.assertAlmostEqual(ab_stats.std([1, 2, 3]), 1.0)
        self.assertEqual(ab_stats.std([5]), 0.0)  # 单点无方差

    def test_confidence_interval(self) -> None:
        ci_low, ci_high = ab_stats.confidence_interval([1, 1, 1])
        # 无方差 → CI = 均值
        self.assertAlmostEqual(ci_low, 1.0)
        self.assertAlmostEqual(ci_high, 1.0)
        # 有方差 → CI 宽于单点
        ci_low2, ci_high2 = ab_stats.confidence_interval([0.5, 0.7, 0.6, 0.8, 0.6])
        self.assertLess(ci_low2, ci_high2)

    def test_win_when_candidate_significantly_better(self) -> None:
        """候选显著更优（CI 下界 > prod 均值）→ win。"""
        prod = [0.5, 0.5, 0.5, 0.5, 0.5]  # 均值 0.5
        cand = [0.8, 0.85, 0.82, 0.83, 0.84]  # 均值 ~0.83，远高于 0.5
        result = ab_stats.decide_verdict(prod, cand)
        self.assertEqual(result["verdict"], "win")
        self.assertGreater(result["mean_cand"], result["mean_prod"])
        self.assertGreater(result["ci_low"], 0.5)  # CI 下界高于 prod 均值

    def test_lose_when_candidate_significantly_worse(self) -> None:
        """候选显著更差 → lose。"""
        prod = [0.8, 0.82, 0.81, 0.83, 0.82]  # 均值 ~0.816
        cand = [0.5, 0.48, 0.52, 0.49, 0.51]  # 均值 ~0.5
        result = ab_stats.decide_verdict(prod, cand)
        self.assertEqual(result["verdict"], "lose")

    def test_tie_when_overlapping(self) -> None:
        """CI 与 prod 均值重叠 → tie。"""
        prod = [0.6, 0.65, 0.62, 0.63, 0.61]  # 均值 ~0.622
        cand = [0.63, 0.60, 0.64, 0.62, 0.61]  # 均值 ~0.62，与 prod 接近
        result = ab_stats.decide_verdict(prod, cand)
        self.assertEqual(result["verdict"], "tie")

    def test_both_no_variance_win(self) -> None:
        """两组都无方差的退化：纯比均值。"""
        prod = [0.5, 0.5, 0.5]
        cand = [0.8, 0.8, 0.8]
        result = ab_stats.decide_verdict(prod, cand)
        self.assertEqual(result["verdict"], "win")

    def test_both_no_variance_tie(self) -> None:
        prod = [0.5, 0.5, 0.5]
        cand = [0.5, 0.5, 0.5]
        result = ab_stats.decide_verdict(prod, cand)
        self.assertEqual(result["verdict"], "tie")

    def test_result_has_all_stat_fields(self) -> None:
        """S11：结果含完整统计量字段。"""
        result = ab_stats.decide_verdict([0.5, 0.6], [0.7, 0.8])
        for field in ("mean_prod", "std_prod", "mean_cand", "std_cand",
                      "ci_low", "ci_high", "p_value_approx", "verdict", "confidence"):
            self.assertIn(field, result)

    def test_ci_widens_with_more_variance(self) -> None:
        """方差越大，CI 越宽（N 固定时）。"""
        narrow = ab_stats.confidence_interval([0.6, 0.61, 0.6, 0.61, 0.6])
        wide = ab_stats.confidence_interval([0.4, 0.8, 0.5, 0.9, 0.6])
        self.assertGreater(wide[1] - wide[0], narrow[1] - narrow[0])  # wide 更宽


if __name__ == "__main__":
    unittest.main()
