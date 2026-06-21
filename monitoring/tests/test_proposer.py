"""Phase 4 T4.1：proposer 测试（D4 + S14 失败重试）。

覆盖：
- _extract_code：从 LLM 输出提取代码（python块/裸块/纯代码）
- generate_candidate_harness：mock LLM 生成
- propose_with_retry：失败重试逻辑（前N次失败，最后成功）
- save_candidate：写文件 + 建版本记录
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class ProposerTest(unittest.TestCase):
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
            self.db.get_conn().execute("DELETE FROM harness_versions")
            self.db.get_conn().execute("DELETE FROM failure_signatures")
            self.db.get_conn().commit()
            self.db.get_conn().close()
        except Exception:
            pass

    def _make_signature(self) -> int:
        cur = self.db.execute(
            """INSERT INTO failure_signatures
               (layer, target, metric, signature_text, target_component, target_ref,
                status, badcase_count, created_at, updated_at)
               VALUES ('content', 'novel', '爽点密度', '升级后无爽点',
                       'prompt', 'writing_system', 'open', 10, '2026-01-01', '2026-01-01')"""
        )
        return cur.lastrowid

    # ── _extract_code ─────────────────────────────────────────

    def test_extract_code_python_block(self) -> None:
        from app import proposer
        raw = "好的：\n```python\nclass H(WriterHarness):\n    pass\n```\n完成"
        code = proposer._extract_code(raw)
        self.assertIn("class H", code)

    def test_extract_code_plain_block(self) -> None:
        from app import proposer
        raw = "```\nclass H(WriterHarness):\n    pass\n```"
        code = proposer._extract_code(raw)
        self.assertIn("class H", code)

    def test_extract_code_raw_output(self) -> None:
        """LLM 直接输出代码（无代码块包裹）。"""
        from app import proposer
        raw = "import x\nclass H(WriterHarness):\n    pass"
        code = proposer._extract_code(raw)
        self.assertIsNotNone(code)

    def test_extract_code_invalid_returns_none(self) -> None:
        from app import proposer
        self.assertIsNone(proposer._extract_code("这是纯文本，无代码"))

    # ── generate_candidate_harness ────────────────────────────

    def test_generate_with_mock_llm(self) -> None:
        from app import proposer
        signature = {
            "signature_text": "升级后无爽点", "target_component": "prompt",
            "target_ref": "writing_system", "root_cause": "缺爽点指令",
            "layer": "content", "target": "novel", "metric": "爽点密度",
            "badcase_count": 10,
        }
        fake_code = "```python\nfrom app.platform.harness import WriterHarness\nclass H(WriterHarness):\n    pass\n```"
        with patch("app.llm.chat", return_value=fake_code), \
             patch("app.llm.judge_enabled", return_value=True):
            code = proposer.generate_candidate_harness(signature, "current code")
        self.assertIsNotNone(code)
        self.assertIn("class H", code)

    def test_generate_no_llm_returns_none(self) -> None:
        from app import proposer
        with patch("app.llm.judge_enabled", return_value=False):
            code = proposer.generate_candidate_harness({}, "code")
        self.assertIsNone(code)

    # ── propose_with_retry ────────────────────────────────────

    def test_retry_succeeds_on_third_attempt(self) -> None:
        """S14：前2次校验失败，第3次成功。"""
        from app import proposer
        sig_id = self._make_signature()
        calls = {"n": 0}

        def fake_validate(code):
            calls["n"] += 1
            if calls["n"] < 3:
                return (False, f"第{calls['n']}次失败：语法错")
            return (True, "")

        fake_code = "```python\nclass H(WriterHarness):\n    pass\n```"
        with patch("app.llm.chat", return_value=fake_code), \
             patch("app.llm.judge_enabled", return_value=True):
            result = proposer.propose_with_retry(sig_id, "current", validate_fn=fake_validate)

        self.assertIsNotNone(result)
        self.assertEqual(result["attempts"], 3)
        self.assertIsNone(result["final_error"])
        self.assertEqual(calls["n"], 3)

    def test_retry_all_fail(self) -> None:
        """3 次都失败 → 返回 None code + final_error。"""
        from app import proposer
        sig_id = self._make_signature()

        def always_fail(code):
            return (False, "总是失败")

        fake_code = "```python\nclass H(WriterHarness):\n    pass\n```"
        with patch("app.llm.chat", return_value=fake_code), \
             patch("app.llm.judge_enabled", return_value=True):
            result = proposer.propose_with_retry(sig_id, "current", validate_fn=always_fail)

        self.assertIsNotNone(result)
        self.assertIsNone(result["code"])
        self.assertEqual(result["attempts"], 3)
        self.assertIn("总是失败", result["final_error"])

    def test_retry_no_validate_fn_accepts_first(self) -> None:
        """无 validate_fn → 第一次生成即接受。"""
        from app import proposer
        sig_id = self._make_signature()
        fake_code = "```python\nclass H(WriterHarness):\n    pass\n```"
        with patch("app.llm.chat", return_value=fake_code), \
             patch("app.llm.judge_enabled", return_value=True):
            result = proposer.propose_with_retry(sig_id, "current", validate_fn=None)
        self.assertEqual(result["attempts"], 1)

    # ── save_candidate ────────────────────────────────────────

    def test_save_candidate_writes_file_and_record(self) -> None:
        from app import proposer
        from app import harness_repo
        # 先建一个 production 版本作为 parent
        parent = harness_repo.create_version("/p/v1", labels=["production"])
        sig_id = self._make_signature()

        code = "class CandidateHarness(WriterHarness):\n    pass\n"
        result = proposer.save_candidate(
            sig_id, code, self._harnesses_root,
            parent_version=parent["version"],
            proposer_meta={"model": "test", "attempts": 1},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "proposed")
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["signature_id"], sig_id)
        # 代码文件已写
        from pathlib import Path
        code_path = Path(result["code_path"])
        self.assertTrue(code_path.exists())
        self.assertIn("class CandidateHarness", code_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
