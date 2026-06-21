"""Phase 3 T3.2：Mining 引擎测试（D8/D12/D14/D15）。

覆盖：
- record_badcase / record_badcases_from_evaluation（D20 立即写表）
- find_dims_ready_to_mine（攒够阈值才返回）
- mine_signature（mock LLM 提炼签名 + 组件归因 S10）
- match_signature（mock LLM 确认同病灶 D15）
- check_and_mine_all（后台触发）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MiningTest(unittest.TestCase):
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
        # 用唯一 trace_id 避免跨测试冲突（INSERT OR IGNORE 幂等）
        self._trace_counter = getattr(type(self), "_tc", 0) + 1
        type(self)._tc = self._trace_counter
        self.trace_id = f"trace-{self._trace_counter}"
        db.execute(
            "INSERT INTO runs (trace_id, workspace_id, status, ingested_at) VALUES (?, ?, ?, ?)",
            (self.trace_id, "ws-1", "completed", "2026-01-01T00:00:00Z"),
        )

    def tearDown(self) -> None:
        try:
            self.db.get_conn().execute("DELETE FROM badcase_records")
            self.db.get_conn().execute("DELETE FROM failure_signatures")
            self.db.get_conn().commit()
            self.db.get_conn().close()
        except Exception:
            pass

    def _add_badcase(self, trace_id=None, layer="content", target="novel",
                     metric="爽点密度", score=0.3, evidence="升级后无爽点") -> None:
        from app import mining
        mining.record_badcase(trace_id or self.trace_id, layer, target, metric, score, evidence)

    def test_record_badcase(self) -> None:
        from app import mining
        rec = mining.record_badcase(self.trace_id, "content", "novel", "爽点密度", 0.3, "无爽点")
        self.assertIsNotNone(rec["id"])
        rows = self.db.query_all("SELECT * FROM badcase_records")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["signature_id"])  # 待匹配

    def test_record_badcases_from_evaluation(self) -> None:
        from app import mining
        badcase_result = {
            "is_badcase": True,
            "flagged_dimensions": [
                {"layer": "content", "target": "novel", "metric": "爽点密度", "score": 0.3, "evidence": "e1"},
                {"layer": "subagent", "target": "writing", "metric": "爽点演绎能力", "score": 0.4, "evidence": "e2"},
            ],
        }
        count = mining.record_badcases_from_evaluation(self.trace_id, badcase_result)
        self.assertEqual(count, 2)

    def test_find_dims_ready_to_mine(self) -> None:
        """攒够阈值才返回该维度。"""
        from app import mining
        # 加 9 条（不够阈值 10）
        for i in range(9):
            self._add_badcase(evidence=f"e{i}")
        ready = mining.find_dims_ready_to_mine(threshold=10)
        self.assertEqual(len(ready), 0)
        # 加第 10 条
        self._add_badcase(evidence="e9")
        ready = mining.find_dims_ready_to_mine(threshold=10)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["metric"], "爽点密度")

    def test_mine_signature_with_mock_llm(self) -> None:
        """mock LLM 提炼签名 → 创建签名 + 关联 badcase。"""
        from app import mining
        for i in range(10):
            self._add_badcase(evidence=f"升级后连续{i}章无爽点")

        fake_response = (
            '{"signature_text": "writing subagent 在升级后连续3+章无爽点", '
            '"target_component": "prompt", '
            '"target_ref": "writing_system", '
            '"root_cause": "prompt 未强调升级后必须紧跟爽点"}'
        )
        with patch("app.llm.chat", return_value=fake_response), \
             patch("app.llm.judge_enabled", return_value=True):
            result = mining.mine_signature("content", "novel", "爽点密度")

        self.assertIsNotNone(result)
        self.assertEqual(result["target_component"], "prompt")
        self.assertEqual(result["target_ref"], "writing_system")
        self.assertEqual(result["badcase_count"], 10)

        # badcase 已关联到签名
        from app import mining as m
        bcs = m.list_badcases()
        self.assertTrue(all(b["signature_id"] is not None for b in bcs))

    def test_mine_signature_threshold_not_met(self) -> None:
        """不够阈值不提炼。"""
        from app import mining
        self._add_badcase()
        result = mining.mine_signature("content", "novel", "爽点密度")
        self.assertIsNone(result)

    def test_match_signature_no_candidates(self) -> None:
        """无候选签名 → 返回 None。"""
        from app import mining
        sig_id = mining.match_signature("content", "novel", "爽点密度", "新证据")
        self.assertIsNone(sig_id)

    def test_match_signature_single_candidate_no_llm(self) -> None:
        """单候选且无 LLM → 直接归入。"""
        from app import mining
        # 先创建一个签名
        self.db.execute(
            """INSERT INTO failure_signatures
               (layer, target, metric, signature_text, target_component, target_ref,
                status, badcase_count, created_at, updated_at)
               VALUES ('content', 'novel', '爽点密度', '签名1', 'prompt', 'writing_system',
                       'open', 10, '2026-01-01', '2026-01-01')"""
        )
        with patch("app.llm.judge_enabled", return_value=False):
            sig_id = mining.match_signature("content", "novel", "爽点密度", "新证据")
        self.assertIsNotNone(sig_id)

    def test_check_and_mine_all(self) -> None:
        """后台触发：攒够的维度被提炼。"""
        from app import mining
        for i in range(10):
            self._add_badcase(evidence=f"e{i}")
        fake_response = (
            '{"signature_text": "sig", "target_component": "prompt", '
            '"target_ref": "writing_system", "root_cause": "rc"}'
        )
        with patch("app.llm.chat", return_value=fake_response), \
             patch("app.llm.judge_enabled", return_value=True):
            signatures = mining.check_and_mine_all(threshold=10)
        self.assertEqual(len(signatures), 1)

    def test_list_signatures_and_badcases(self) -> None:
        from app import mining
        self._add_badcase()
        self.assertEqual(len(mining.list_badcases()), 1)
        self.assertEqual(len(mining.list_signatures()), 0)

    def test_record_badcases_preserves_evidence(self) -> None:
        """record_badcases_from_evaluation 保留 evidence（Mining 提炼签名的关键输入）。"""
        from app import mining
        badcase_result = {
            "is_badcase": True,
            "flagged_dimensions": [
                {"layer": "content", "target": "novel", "metric": "爽点密度",
                 "score": 0.3, "evidence": "升级后连续5章无爽点，节奏拖沓"},
            ],
        }
        mining.record_badcases_from_evaluation(self.trace_id, badcase_result)
        bcs = mining.list_badcases()
        self.assertEqual(bcs[0]["evidence"], "升级后连续5章无爽点，节奏拖沓")


if __name__ == "__main__":
    unittest.main()
