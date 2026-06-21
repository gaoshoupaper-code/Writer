"""Phase 4 T4.5：流水线编排测试（S12 分段自动化 + S13 + D17 批准）。

覆盖：
- process_signature：签名 → proposer（mock）→ 静态检查 → 建候选版本
- run_ab_experiment：mock 生成函数 → 统计判定 → 存结果
- approve_experiment：win → promote production
- reject_experiment：签名回 open
- run_pipeline_cycle：后台触发
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_VALID_CODE = '''from app.platform.harness import WriterHarness


class TestCandidateHarness(WriterHarness):
    def build_system_prompt(self, ctx):
        return "p"
    def build_skills(self, ctx):
        return []
    def build_middleware(self, ctx):
        return []
    def build_subagents(self, ctx):
        return []
'''


class PipelineTest(unittest.TestCase):
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
        # 初始化测试集
        from app import replay
        replay.ensure_default_multistyle_test_set()
        # 建 production harness v1 + 代码文件
        from app import harness_repo
        code_path = harness_repo.write_harness_code(
            self._harnesses_root, _VALID_CODE, 1
        )
        self.prod = harness_repo.create_version(
            str(code_path), labels=["production"], status="approved"
        )

    def tearDown(self) -> None:
        try:
            for t in ("harness_versions", "harness_experiments", "failure_signatures"):
                self.db.get_conn().execute(f"DELETE FROM {t}")
            self.db.get_conn().commit()
            self.db.get_conn().close()
        except Exception:
            pass

    def _make_signature(self, status="open") -> int:
        cur = self.db.execute(
            """INSERT INTO failure_signatures
               (layer, target, metric, signature_text, target_component, target_ref,
                status, badcase_count, created_at, updated_at)
               VALUES ('content', 'novel', '爽点密度', '升级后无爽点',
                       'prompt', 'writing_system', ?, 10, '2026-01-01', '2026-01-01')""",
            (status,),
        )
        return cur.lastrowid

    # ── process_signature ─────────────────────────────────────

    def test_process_signature_success(self) -> None:
        """签名 → proposer（mock出合法代码）→ 静态检查过 → 建候选版本。"""
        from app import pipeline
        sig_id = self._make_signature()
        with patch("app.llm.chat", return_value=f"```python\n{_VALID_CODE}\n```"), \
             patch("app.llm.judge_enabled", return_value=True):
            result = pipeline.process_signature(sig_id, str(self._harnesses_root))
        self.assertEqual(result["status"], "static_checked")
        self.assertIsNotNone(result["candidate_version"])
        # 签名标记 proposed
        sig = self.db.query_one("SELECT status FROM failure_signatures WHERE id=?", (sig_id,))
        self.assertEqual(sig["status"], "proposed")

    def test_process_signature_propose_fail(self) -> None:
        """proposer 全失败 → 签名回 open。"""
        from app import pipeline
        sig_id = self._make_signature()
        # mock LLM 返回无效代码
        with patch("app.llm.chat", return_value="无效文本"), \
             patch("app.llm.judge_enabled", return_value=True):
            result = pipeline.process_signature(sig_id, str(self._harnesses_root))
        self.assertEqual(result["status"], "propose_failed")
        sig = self.db.query_one("SELECT status FROM failure_signatures WHERE id=?", (sig_id,))
        self.assertEqual(sig["status"], "open")

    def test_process_signature_static_check_fail(self) -> None:
        """proposer 出危险代码（os.system）→ 校验阶段拦截 → propose_failed。

        process_signature 用 static_check 作 validate_fn，危险代码在 propose
        重试阶段就被拒（3次都失败），走 propose_failed 分支。这是双重保险的
        初筛在 propose 阶段就生效。签名回 open。
        """
        from app import pipeline
        sig_id = self._make_signature()
        bad_code = _VALID_CODE + "\nimport os\nos.system('rm -rf /')\n"
        with patch("app.llm.chat", return_value=f"```python\n{bad_code}\n```"), \
             patch("app.llm.judge_enabled", return_value=True):
            result = pipeline.process_signature(sig_id, str(self._harnesses_root))
        # 危险代码被 static_check（作 validate_fn）在 propose 阶段拦截
        self.assertEqual(result["status"], "propose_failed")
        sig = self.db.query_one("SELECT status FROM failure_signatures WHERE id=?", (sig_id,))
        self.assertEqual(sig["status"], "open")

    def test_process_signature_skips_non_open(self) -> None:
        """非 open 状态的签名被跳过。"""
        from app import pipeline
        sig_id = self._make_signature(status="proposed")
        result = pipeline.process_signature(sig_id, str(self._harnesses_root))
        # 直接返回 None（跳过）
        self.assertIsNone(result)

    # ── run_ab_experiment ─────────────────────────────────────

    def test_run_ab_with_mock_generation(self) -> None:
        """mock 生成函数 → 收集分数 → 统计判定。"""
        from app import pipeline
        # 先建一个候选
        from app import harness_repo
        cand_code = harness_repo.write_harness_code(
            self._harnesses_root, _VALID_CODE, 99
        )
        cand = harness_repo.create_version(
            str(cand_code), labels=["candidate"], status="static_checked"
        )

        # mock 生成函数：返回假 trace_id，并预置评估分数
        call_count = {"n": 0}

        def fake_gen(request, version, item):
            call_count["n"] += 1
            trace_id = f"fake-trace-{call_count['n']}"
            # 预置评估分数（candidate 版本给高分，prod 给低分）
            score = 0.85 if version == cand["version"] else 0.5
            self.db.execute(
                "INSERT INTO runs (trace_id, workspace_id, status, ingested_at) VALUES (?,?,?,?)",
                (trace_id, "ws", "completed", "2026-01-01"),
            )
            self.db.execute(
                "INSERT INTO evaluation_scores (trace_id, layer, target, metric, score, evidence, scored_at) VALUES (?,?,?,?,?,?,'2026-01-01')",
                (trace_id, "content", "novel", "m", score, "e"),
            )
            return trace_id

        result = pipeline.run_ab_experiment(
            cand["version"], run_generation_fn=fake_gen, seed_count=2,
        )
        self.assertEqual(result["verdict"], "win")  # candidate 高分
        self.assertEqual(result["seed_count"], 2)

    def test_run_ab_pending_when_no_gen_fn(self) -> None:
        """无生成函数 → 返回 pending_execution（待后台执行）。"""
        from app import pipeline
        from app import harness_repo
        cand_code = harness_repo.write_harness_code(
            self._harnesses_root, _VALID_CODE, 98
        )
        cand = harness_repo.create_version(
            str(cand_code), labels=["candidate"], status="static_checked"
        )
        result = pipeline.run_ab_experiment(cand["version"], seed_count=2)
        self.assertEqual(result["status"], "pending_execution")

    # ── approve / reject ──────────────────────────────────────

    def test_approve_promotes_candidate(self) -> None:
        """win 实验 → 批准 → candidate 升 production。"""
        from app import pipeline, harness_repo
        # 建候选 + 实验（win）
        cand = harness_repo.create_version(
            "/p/cand", labels=["candidate"], status="ab_testing"
        )
        sig_id = self._make_signature(status="proposed")
        self.db.execute(
            """INSERT INTO harness_experiments
               (candidate_version, prod_version, signature_id, seed_count,
                verdict, status, created_at)
               VALUES (?, ?, ?, ?, 'win', 'done', '2026-01-01')""",
            (cand["version"], 1, sig_id, 2),
        )
        exp = self.db.query_one("SELECT id FROM harness_experiments WHERE candidate_version=?", (cand["version"],))

        result = pipeline.approve_experiment(exp["id"])
        self.assertEqual(result["status"], "approved")
        # candidate 现在是 production
        prod = harness_repo.get_production_version()
        self.assertEqual(prod["version"], cand["version"])
        # 签名 resolved
        sig = self.db.query_one("SELECT status FROM failure_signatures WHERE id=?", (sig_id,))
        self.assertEqual(sig["status"], "resolved")

    def test_approve_rejects_non_win(self) -> None:
        """非 win 的实验不能批准。"""
        from app import pipeline
        from app import harness_repo
        cand = harness_repo.create_version("/p/cand2", labels=["candidate"])
        self.db.execute(
            """INSERT INTO harness_experiments
               (candidate_version, prod_version, seed_count, verdict, status, created_at)
               VALUES (?, ?, ?, 'lose', 'done', '2026-01-01')""",
            (cand["version"], 1, 2),
        )
        exp = self.db.query_one("SELECT id FROM harness_experiments WHERE candidate_version=?", (cand["version"],))
        with self.assertRaises(ValueError):
            pipeline.approve_experiment(exp["id"])

    def test_reject_returns_signature_to_open(self) -> None:
        """拒绝 → 候选 rejected + 签名回 open。"""
        from app import pipeline, harness_repo
        cand = harness_repo.create_version("/p/cand3", labels=["candidate"], status="ab_testing")
        sig_id = self._make_signature(status="proposed")
        self.db.execute(
            """INSERT INTO harness_experiments
               (candidate_version, prod_version, signature_id, seed_count,
                verdict, status, created_at)
               VALUES (?, ?, ?, ?, 'tie', 'done', '2026-01-01')""",
            (cand["version"], 1, sig_id, 2),
        )
        exp = self.db.query_one("SELECT id FROM harness_experiments WHERE candidate_version=?", (cand["version"],))
        result = pipeline.reject_experiment(exp["id"])
        self.assertEqual(result["status"], "rejected")
        sig = self.db.query_one("SELECT status FROM failure_signatures WHERE id=?", (sig_id,))
        self.assertEqual(sig["status"], "open")


if __name__ == "__main__":
    unittest.main()
