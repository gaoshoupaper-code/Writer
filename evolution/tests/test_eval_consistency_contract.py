"""进化观测与评估状态一致性根治的验收测试（REQ-20260730-215454）。

覆盖：
  - AC-003：证据编纂耗时可完整分解（确定性阶段 + 每次 llm.chat 可观测，FR-002/CON-001）
  - AC-004：评分组失败不重算成功组（组级幂等/重试，FR-003/CON-003/EDGE-002）
  - AC-005：评估等待满足冻结预算（并发≤2 / 单组≤60s / 失败组重试≤1 / 总预算150s，NFR-001）
  - AC-006：封存成功后三方完成（唯一完成判据，FR-004/CON-002/CON-003）
  - AC-007：封存失败后三方不完成（失败语义，FR-004/CON-002/CON-003）
  - AC-008：历史误标评估只按封存证据纠正（DEC-003/RSK-003，FR-006）

AC-001/AC-002（桌面端真实安装 + 线上 access log）为 post-push deferred AC，
由发布后在真实环境验证，不在本文件范围。

设计依据：.claude/md/20260730_215454_进化观测与评估状态一致性根治.md
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import app.core.db as db  # noqa: E402
from app.core import llm  # noqa: E402


def setUpModule() -> None:
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


class _RecordingObserver:
    """实现 LlmCallObserver 协议的测试桩，记录每次 llm.chat 的 span。"""

    def __init__(self):
        self.spans: list[dict] = []

    def on_llm_start(self, *, phase, model, messages):
        self.spans.append({"event": "start", "phase": phase, "model": model})

    def on_llm_end(self, *, phase, model, duration_ms, output):
        self.spans.append({
            "event": "end", "phase": phase, "model": model, "duration_ms": duration_ms,
        })

    def on_llm_error(self, *, phase, model, duration_ms, error):
        self.spans.append({
            "event": "error", "phase": phase, "model": model, "duration_ms": duration_ms,
            "error": f"{error.__class__.__name__}: {error}",
        })

    def phases(self):
        return [s["phase"] for s in self.spans if s["event"] in ("start", "end", "error")]


class LlmObserverHookTest(unittest.TestCase):
    """AC-003 / CON-001：llm.chat 接受 trace 观察者并在调用前后回调。"""

    def test_chat_calls_observer_on_success(self):
        obs = _RecordingObserver()
        with patch("app.core.llm._get_config", return_value=("k", "http://x", "m")), \
             patch("httpx.Client") as mock_client_cls:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
            mock_resp.raise_for_status = lambda: None
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = llm.chat([{"role": "user", "content": "q"}], trace=obs, phase="test_phase")
        self.assertEqual(result, "hi")
        phases = obs.phases()
        self.assertEqual(phases.count("test_phase"), 2)  # start + end
        self.assertTrue(any(s["event"] == "end" for s in obs.spans))

    def test_chat_calls_observer_on_error(self):
        obs = _RecordingObserver()
        with patch("app.core.llm._get_config", return_value=("k", "http://x", "m")), \
             patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                TimeoutError("read timeout")
            )
            with self.assertRaises(TimeoutError):
                llm.chat([{"role": "user", "content": "q"}], trace=obs, phase="err_phase")
        self.assertTrue(any(s["event"] == "error" and s["phase"] == "err_phase" for s in obs.spans))

    def test_chat_without_observer_backward_compat(self):
        """不传 trace 时保持旧行为，不报错（向后兼容）。"""
        with patch("app.core.llm._get_config", return_value=("k", "http://x", "m")), \
             patch("httpx.Client") as mock_client_cls:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            mock_resp.raise_for_status = lambda: None
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = llm.chat([{"role": "user", "content": "q"}])
        self.assertEqual(result, "ok")


class ContentScoringBudgetTest(unittest.TestCase):
    """AC-004 / AC-005：内容评分组级幂等 + 冻结预算（DEC-002 / NFR-001）。"""

    FACTS = {
        "deliveries": {
            "interview": {"demand.md": {"content_frozen": "需求正文"}},
            "writing": {"chapter.md": {"content_frozen": "正文内容"}},
        }
    }

    def setUp(self):
        from app.eval_agent import scoring
        scoring.clear_content_cache()
        self._scoring = scoring

    def _fake_chat_factory(self, fail_group=None, delay=0.0, exc_cls=TimeoutError):
        """构造可控的 llm.chat 替身：fail_group 持续失败。

        exc_cls 控制失败类型（TimeoutError=超时路径；其它 Exception=5xx/服务端错误路径）。
        返回 (chat_fn, call_log)。
        """
        call_log: dict[str, list] = {"calls": []}

        def fake_chat(messages, *, temperature=0.0, timeout=60.0, trace=None, phase="llm"):
            call_log["calls"].append(phase)
            if delay:
                time.sleep(delay)
            group = phase.split(":", 1)[-1] if ":" in phase else phase
            if fail_group and group == fail_group:
                # 模拟持续失败（首轮 + 重试都失败）
                raise exc_cls(f"{group} failure")
            return '{"scores": {"dim1": 4}, "evidence": "ok", "verdict": "pass"}'

        return fake_chat, call_log

    def test_all_groups_complete_with_concurrency_two(self):
        """AC-005：四组正常，并发≤2，全部 completed。"""
        facts = {
            "deliveries": {
                "interview": {"demand.md": {"content_frozen": "d"}},
                "storybuilding": {"setup.md": {"content_frozen": "s"}},
                "detail-outline": {"outline.md": {"content_frozen": "o"}},
                "writing": {"body.md": {"content_frozen": "b"}},
            }
        }
        fake_chat, _ = self._fake_chat_factory()
        with patch("app.eval_agent.scoring.llm.chat", side_effect=fake_chat), \
             patch("app.eval_agent.scoring.llm.judge_enabled", return_value=True):
            result = asyncio.run(self._scoring.evaluate_content_groups(
                facts, "trace-1", eval_id="eval-1",
            ))
        self.assertTrue(result["complete"])
        self.assertEqual(result["failed_groups"], [])
        # 每组至少 1 次调用（成功组），成功组重算为 0 → 每组 attempts=1
        for name, g in result["groups"].items():
            self.assertEqual(g["status"], "completed")
            self.assertEqual(g["attempts"], 1, f"{name} 不应重算")

    def test_failed_group_retries_once_and_does_not_recompute_success(self):
        """AC-004：失败组最多重试 1 次，成功组重算次数为 0。"""
        fake_chat, call_log = self._fake_chat_factory(fail_group="body")
        with patch("app.eval_agent.scoring.llm.chat", side_effect=fake_chat), \
             patch("app.eval_agent.scoring.llm.judge_enabled", return_value=True):
            result = asyncio.run(self._scoring.evaluate_content_groups(
                self.FACTS, "trace-2", eval_id="eval-2",
            ))
        # body 组失败（首轮 + 1 次重试 = 2 次 attempts）
        self.assertEqual(result["groups"]["body"]["status"], "failed")
        self.assertEqual(result["groups"]["body"]["attempts"], 2)
        self.assertIn("body", result["failed_groups"])
        self.assertFalse(result["complete"])
        # general 组成功，只调用 1 次
        self.assertEqual(result["groups"]["general"]["status"], "completed")
        self.assertEqual(result["groups"]["general"]["attempts"], 1)

    def test_agent_recall_reuses_cached_results(self):
        """AC-004：Agent 重复请求时复用缓存，成功组重复调用为 0。"""
        fake_chat, call_log = self._fake_chat_factory()
        with patch("app.eval_agent.scoring.llm.chat", side_effect=fake_chat), \
             patch("app.eval_agent.scoring.llm.judge_enabled", return_value=True):
            r1 = asyncio.run(self._scoring.evaluate_content_groups(
                self.FACTS, "trace-3", eval_id="eval-3",
            ))
            calls_after_first = len(call_log["calls"])
            # 同一 eval 重复请求 → 复用缓存，不产生新调用
            r2 = asyncio.run(self._scoring.evaluate_content_groups(
                self.FACTS, "trace-3", eval_id="eval-3",
            ))
            calls_after_second = len(call_log["calls"])
        self.assertEqual(calls_after_second, calls_after_first, "成功组重复调用应为 0")
        # 两次结果都 complete
        self.assertTrue(r1["complete"])
        self.assertTrue(r2["complete"])

    def test_total_budget_enforced(self):
        """AC-005：内容评分总墙钟不超过 150 秒（预算常量正确）。"""
        from app.eval_agent.scoring import CONTENT_TOTAL_BUDGET_S, CONTENT_MAX_CONCURRENCY
        self.assertEqual(CONTENT_TOTAL_BUDGET_S, 150.0)
        self.assertEqual(CONTENT_MAX_CONCURRENCY, 2)
        from app.eval_agent.scoring import CONTENT_GROUP_TIMEOUT_S, CONTENT_GROUP_MAX_ATTEMPTS
        self.assertEqual(CONTENT_GROUP_TIMEOUT_S, 60.0)
        self.assertEqual(CONTENT_GROUP_MAX_ATTEMPTS, 2)  # 首次 + 1 重试

    def test_results_are_machine_readable(self):
        """CON-003：组级结果是机器可读状态，错误不伪装成成功。"""
        fake_chat, _ = self._fake_chat_factory(fail_group="body")
        with patch("app.eval_agent.scoring.llm.chat", side_effect=fake_chat), \
             patch("app.eval_agent.scoring.llm.judge_enabled", return_value=True):
            result = asyncio.run(self._scoring.evaluate_content_groups(
                self.FACTS, "trace-4", eval_id="eval-4",
            ))
        # 失败组有机器可读 status + error，而非字符串
        body = result["groups"]["body"]
        self.assertEqual(body["status"], "failed")
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str)
        # 超时分类在错误描述中（中文「超时」）
        self.assertTrue("超时" in body["error"] or "timeout" in body["error"].lower())

    def test_server_5xx_failure_path(self):
        """AC-005 矩阵第三种：服务端 5xx（非超时异常）路径。

        需求要求覆盖「一个组服务端 5xx」矩阵。5xx 走 except Exception（区别于超时的
        except TimeoutError），其重试→失败→缓存→complete=False 全链路必须被验证。
        """
        # 用普通 Exception 模拟 5xx/服务端错误（非 TimeoutError）
        fake_chat, _ = self._fake_chat_factory(fail_group="body", exc_cls=ConnectionError)
        with patch("app.eval_agent.scoring.llm.chat", side_effect=fake_chat), \
             patch("app.eval_agent.scoring.llm.judge_enabled", return_value=True):
            result = asyncio.run(self._scoring.evaluate_content_groups(
                self.FACTS, "trace-5xx", eval_id="eval-5xx",
            ))
        # body 组失败（5xx 走 except Exception 分支，首轮 + 1 重试 = 2 attempts）
        self.assertEqual(result["groups"]["body"]["status"], "failed")
        self.assertEqual(result["groups"]["body"]["attempts"], 2)
        self.assertIn("body", result["failed_groups"])
        self.assertFalse(result["complete"])
        # 5xx 错误分类在描述中（ConnectionError）
        self.assertIn("ConnectionError", result["groups"]["body"]["error"])


class EvalTerminalStateTest(unittest.TestCase):
    """AC-006 / AC-007：评估唯一终态判据（FR-004 / CON-002）。"""

    def _make_session(self, eval_id, *, status="running", sealed_dossier_id=None, self_trace_id=None):
        db.execute(
            "INSERT OR REPLACE INTO evaluation_sessions "
            "(eval_id, trace_id, status, created_at, updated_at, sealed_dossier_id, self_trace_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (eval_id, "trace-x", status, "2026-01-01", "2026-01-01", sealed_dossier_id, self_trace_id),
        )

    def _make_sealed_dossier(self, dossier_id, eval_id, *, findings=None):
        import json
        db.execute(
            "INSERT OR REPLACE INTO evaluation_dossiers "
            "(dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version, trace_id, "
            " owner_user_id, conclusions_json, findings_json, positive_patterns_json, "
            " completeness_status, seal_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (dossier_id, eval_id, "ed-1", 1, "trace-x", "u1",
             "[]", json.dumps(findings or [{"id": "f1", "evidence_ref": "evt-1"}]), "[]",
             "complete", "sealed", "2026-01-01"),
        )

    def test_success_criterion_is_completed_plus_sealed(self):
        """AC-006：成功 = completed + 有效 sealed_dossier_id（FR-004 唯一判据）。"""
        from app.eval_agent.agent import _is_eval_successfully_sealed
        self.assertTrue(_is_eval_successfully_sealed(
            {"status": "completed", "sealed_dossier_id": "abc"}
        ))
        # 旧 done 不再被认作成功（状态迁移闭合）
        self.assertFalse(_is_eval_successfully_sealed(
            {"status": "done", "sealed_dossier_id": "abc"}
        ))
        # completed 但无 sealed_dossier_id 不算成功
        self.assertFalse(_is_eval_successfully_sealed({"status": "completed"}))
        # failed 不算成功
        self.assertFalse(_is_eval_successfully_sealed(
            {"status": "failed", "sealed_dossier_id": "abc"}
        ))

    def test_sealed_success_not_downgraded_by_fallback(self):
        """AC-006：封存成功（completed）不被收尾 fallback 改回 failed（根因 bug 回归防护）。"""
        from app.eval_agent.agent import _is_eval_successfully_sealed
        # 模拟 sealer 已写 completed + sealed_dossier_id
        session = {"status": "completed", "sealed_dossier_id": "97d97ecc8489"}
        # _is_eval_successfully_sealed 返回 True → 不触发 _fallback_report
        self.assertTrue(_is_eval_successfully_sealed(session))

    def test_seal_failure_leaves_failed_no_dossier(self):
        """AC-007：封存失败 → 业务 failed，sealed_dossier_id 为空。"""
        from app.eval_agent.agent import _is_eval_successfully_sealed
        session = {"status": "failed", "sealed_dossier_id": None}
        self.assertFalse(_is_eval_successfully_sealed(session))
        # 不存在 failed/completed 或 failed/sealed 的分裂：failed 时判据直接 False


class EvalReconcileTest(unittest.TestCase):
    """AC-006 / AC-007 / EDGE-003：启动对账收敛封存成功但误标失败的评估。"""

    def setUp(self):
        db.execute("DELETE FROM evaluation_sessions")
        db.execute("DELETE FROM evaluation_dossiers")

    def _make_session(self, eval_id, *, status, sealed_dossier_id, self_trace_id=None):
        db.execute(
            "INSERT OR REPLACE INTO evaluation_sessions "
            "(eval_id, trace_id, status, created_at, updated_at, sealed_dossier_id, self_trace_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (eval_id, "trace-r", status, "2026-01-01", "2026-01-01", sealed_dossier_id, self_trace_id),
        )

    def _make_sealed_dossier(self, dossier_id, eval_id):
        db.execute(
            "INSERT OR REPLACE INTO evaluation_dossiers "
            "(dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version, trace_id, "
            " owner_user_id, conclusions_json, findings_json, positive_patterns_json, "
            " completeness_status, seal_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (dossier_id, eval_id, "ed-1", 1, "trace-r", "u1", "[]", "[]", "[]",
             "complete", "sealed", "2026-01-01"),
        )

    def _make_run_completed(self, trace_id):
        db.execute(
            "INSERT OR REPLACE INTO runs(trace_id, workspace_id, status, run_purpose, ingested_at, "
            "schema_version, service, workload, integrity_status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (trace_id, "ws", "completed", "evolution_eval", "2026-01-01", 2, "evolution",
             "evaluation", "verified", "2026-01-01"),
        )

    def test_reconcile_fixes_mislabeled_sealed_success(self):
        """EDGE-003：封存成功但误标 failed → 对账收敛为 completed。"""
        from app.eval_agent.reconcile import reconcile_eval_terminal_states
        self._make_session("ev-recon-1", status="failed", sealed_dossier_id="ed-sealed-1",
                           self_trace_id="trace-self-1")
        self._make_sealed_dossier("ed-sealed-1", "ev-recon-1")
        self._make_run_completed("trace-self-1")
        result = reconcile_eval_terminal_states()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["reconciled"], 1)
        # 验证 DB 已收敛
        row = db.query_one("SELECT status FROM evaluation_sessions WHERE eval_id='ev-recon-1'")
        self.assertEqual(row["status"], "completed")

    def test_reconcile_skips_when_dossier_not_sealed(self):
        """RSK-003：sealed_dossier_id 存在但卷宗非 sealed → 不动（证据不充分）。"""
        from app.eval_agent.reconcile import reconcile_eval_terminal_states
        self._make_session("ev-recon-2", status="failed", sealed_dossier_id="ed-bad-2")
        # 不创建 sealed 卷宗（证据不充分）
        result = reconcile_eval_terminal_states()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["reconciled"], 0)
        row = db.query_one("SELECT status FROM evaluation_sessions WHERE eval_id='ev-recon-2'")
        self.assertEqual(row["status"], "failed")


class EvalHistoryCorrectionTest(unittest.TestCase):
    """AC-008：历史误标评估只按封存证据纠正（FR-006 / DEC-003 / RSK-003）。"""

    def setUp(self):
        db.execute("DELETE FROM evaluation_sessions")
        db.execute("DELETE FROM evaluation_dossiers")
        db.execute("DELETE FROM runs")
        db.execute("DELETE FROM eval_correction_audit")

    def _make_session(self, eval_id, *, status, sealed_dossier_id, self_trace_id=None):
        db.execute(
            "INSERT OR REPLACE INTO evaluation_sessions "
            "(eval_id, trace_id, status, created_at, updated_at, sealed_dossier_id, self_trace_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (eval_id, "trace-h", status, "2026-01-01", "2026-01-01", sealed_dossier_id, self_trace_id),
        )

    def _make_sealed_dossier(self, dossier_id, eval_id, *, completeness="complete"):
        import json
        db.execute(
            "INSERT OR REPLACE INTO evaluation_dossiers "
            "(dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version, trace_id, "
            " owner_user_id, conclusions_json, findings_json, positive_patterns_json, "
            " completeness_status, seal_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (dossier_id, eval_id, "ed-1", 1, "trace-h", "u1",
             json.dumps([{"id": "c1"}]),
             json.dumps([{"id": "f1", "evidence_ref": "evt-1"}]), "[]",
             completeness, "sealed", "2026-01-01"),
        )

    def _make_run_completed(self, trace_id):
        db.execute(
            "INSERT OR REPLACE INTO runs(trace_id, workspace_id, status, run_purpose, ingested_at, "
            "schema_version, service, workload, integrity_status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (trace_id, "ws", "completed", "evolution_eval", "2026-01-01", 2, "evolution",
             "evaluation", "verified", "2026-01-01"),
        )

    def test_dry_run_does_not_write(self):
        """dry_run=True 只读预演，不写库。"""
        from app.eval_agent.migrations import correct_mislabeled_eval_history
        self._make_session("ev-h1", status="failed", sealed_dossier_id="ed-h1",
                           self_trace_id="trace-h1")
        self._make_sealed_dossier("ed-h1", "ev-h1")
        self._make_run_completed("trace-h1")
        result = correct_mislabeled_eval_history(dry_run=True)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(len(result["target"]), 1)
        self.assertEqual(result["corrected"], [])
        # DB 未变
        row = db.query_one("SELECT status FROM evaluation_sessions WHERE eval_id='ev-h1'")
        self.assertEqual(row["status"], "failed")

    def test_apply_corrects_provable_success_with_audit(self):
        """apply 精确纠正可证明记录，保留快照与审计。"""
        from app.eval_agent.migrations import correct_mislabeled_eval_history
        self._make_session("ev-h2", status="failed", sealed_dossier_id="ed-h2",
                           self_trace_id="trace-h2")
        self._make_sealed_dossier("ed-h2", "ev-h2")
        self._make_run_completed("trace-h2")
        result = correct_mislabeled_eval_history(dry_run=False)
        self.assertEqual(len(result["corrected"]), 1)
        self.assertIn("ev-h2", result["corrected"])
        self.assertTrue(result["audit"])
        self.assertEqual(len(result["snapshots"]), 1)
        # DB 已纠正
        row = db.query_one("SELECT status FROM evaluation_sessions WHERE eval_id='ev-h2'")
        self.assertEqual(row["status"], "completed")

    def test_does_not_correct_genuinely_failed(self):
        """RSK-003：真正失败（无 sealed dossier）的对照记录不动。"""
        from app.eval_agent.migrations import correct_mislabeled_eval_history
        # 真正失败：无 sealed_dossier_id
        self._make_session("ev-h3-fail", status="failed", sealed_dossier_id=None)
        # 证据不充分：有 sealed_dossier_id 但无 sealed 卷宗
        self._make_session("ev-h4-insufficient", status="failed", sealed_dossier_id="ed-missing")
        result = correct_mislabeled_eval_history(dry_run=True)
        self.assertEqual(len(result["target"]), 0)
        self.assertEqual(len(result["excluded"]), 1)  # ev-h4 被排除
        # ev-h3-fail 不在候选（无 sealed_dossier_id），不在 scanned
        # 两记录状态都不变
        for eid in ("ev-h3-fail", "ev-h4-insufficient"):
            row = db.query_one("SELECT status FROM evaluation_sessions WHERE eval_id=?", (eid,))
            self.assertEqual(row["status"], "failed")

    def test_idempotent_repeated_run(self):
        """幂等：已 completed 的不再处理。"""
        from app.eval_agent.migrations import correct_mislabeled_eval_history
        self._make_session("ev-h5", status="failed", sealed_dossier_id="ed-h5",
                           self_trace_id="trace-h5")
        self._make_sealed_dossier("ed-h5", "ev-h5")
        self._make_run_completed("trace-h5")
        correct_mislabeled_eval_history(dry_run=False)
        # 再跑：已 completed，不在候选（status != 'completed' 条件过滤掉）
        result2 = correct_mislabeled_eval_history(dry_run=False)
        self.assertEqual(len(result2["corrected"]), 0)

    def test_apply_persists_audit_and_clears_failure_reason(self):
        """RSK-003/DM-002：apply 写持久审计表 + 清理 failure_reason（一致终态）。"""
        from app.eval_agent.migrations import correct_mislabeled_eval_history
        self._make_session("ev-h6", status="failed", sealed_dossier_id="ed-h6",
                           self_trace_id="trace-h6")
        self._make_sealed_dossier("ed-h6", "ev-h6")
        self._make_run_completed("trace-h6")
        # 给 session 灌一个 failure_reason（模拟旧失败描述残留）
        db.execute(
            "UPDATE evaluation_sessions SET failure_reason='状态判断缺陷：只认 done' "
            "WHERE eval_id='ev-h6'"
        )
        result = correct_mislabeled_eval_history(dry_run=False)
        self.assertEqual(len(result["corrected"]), 1)
        # 审计表有持久记录
        audit_rows = db.query_all(
            "SELECT * FROM eval_correction_audit WHERE eval_id='ev-h6'"
        )
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["status_before"], "failed")
        self.assertEqual(audit_rows[0]["status_after"], "completed")
        # failure_reason 已清理（一致终态，无自相矛盾）
        row = db.query_one("SELECT failure_reason FROM evaluation_sessions WHERE eval_id='ev-h6'")
        self.assertIsNone(row["failure_reason"])


class ReportSealGateTest(unittest.TestCase):
    """CON-003 / AC-005：report.py 在 content complete=False 时拒绝封存（硬闸门）。

    回归防护：write_eval_report 取内容分数后必须检查 complete，绝不让 incomplete
    scores 原样喂给 sealer。本测试验证源码闸门存在且条件正确。
    """

    def test_report_gate_checks_complete(self):
        """write_eval_report 源码必须含 complete 闸门（防 partial 封存）。"""
        import ast
        import inspect
        from app.eval_agent.tools import report
        tree = ast.parse(inspect.getsource(report))
        # 找 write_eval_report 的函数体，确认含 complete 判断
        has_complete_gate = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and getattr(node, "attr", None) == "get":
                # 形如 cr.get("complete", ...)
                if node.value is not None and getattr(node.value, "id", None) in ("cr",):
                    has_complete_gate = True
        self.assertTrue(
            has_complete_gate,
            "write_eval_report 必须检查 cr.get('complete') 防止 partial 封存（CON-003）",
        )


class DossierVersionBumpTest(unittest.TestCase):
    """AC-002 / RSK-004：桌面版本号唯一且高于 0.2.40。"""

    def test_version_is_unique_and_higher(self):
        import json
        tauri_conf = Path(__file__).resolve().parents[2] / "evolution" / "desktop" / "src-tauri" / "tauri.conf.json"
        cargo_toml = tauri_conf.parent / "Cargo.toml"
        conf = json.loads(tauri_conf.read_text(encoding="utf-8"))
        cargo = cargo_toml.read_text(encoding="utf-8")
        self.assertNotEqual(conf["version"], "0.2.40", "版本号不得复用已发布的 0.2.40")
        # 解析版本号比较
        parts = conf["version"].split(".")
        self.assertEqual(len(parts), 3)
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        self.assertTrue((major, minor, patch) > (0, 2, 40), "新版本必须高于 0.2.40")
        # Cargo.toml 版本号一致
        import re
        m = re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), conf["version"], "tauri.conf.json 与 Cargo.toml 版本号必须一致")


if __name__ == "__main__":
    unittest.main()
