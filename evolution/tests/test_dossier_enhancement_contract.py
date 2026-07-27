"""证据卷宗增强契约测试（2026-07-27，证据分级可见性重构阶段 B）。

固化阶段 B 引入的新机制，防止后续阶段 C/D/E 回退：
  - B1：交付物冻结正文（全文截断 + sha256 指纹）
  - B2：memory_quality 冻结进 facts
  - B3：任务契约驱动覆盖矩阵（确定性，不依赖 Agent 自述）
  - B4：终态卷宗不可变（DossierImmutableError）

设计依据：.claude/md/20260727_174943_进化证据分级可见性重构.md（需求 §32/§33/§35）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402

# 临时工作区（executor workspace 三层路径）
_TMP_WS_DIR = tempfile.mkdtemp()
settings.executor_workspace = _TMP_WS_DIR


def setUpModule() -> None:
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass
    import shutil
    shutil.rmtree(_TMP_WS_DIR, ignore_errors=True)


def _seed_trace(trace_id="trace-b", owner="userB", ws="wsB",
                 demand_text="# 需求\n写玄幻", chapters=None):
    """灌一条最小可用 trace（runs + events + nodes + 工作区文件）。"""
    db.execute(
        "INSERT INTO runs(trace_id, workspace_id, status, owner_user_id, run_purpose, "
        "ingested_at, endpoint) VALUES(?,?,?,?,?,?,?)",
        (trace_id, ws, "completed", owner, "user_generation", "2026-01-01", "create"),
    )
    ws_root = Path(_TMP_WS_DIR) / owner / ws
    ws_root.mkdir(parents=True, exist_ok=True)
    (ws_root / "demand.md").write_text(demand_text, encoding="utf-8")
    if chapters:
        (ws_root / "chapter").mkdir(exist_ok=True)
        for name, text in chapters.items():
            (ws_root / "chapter" / name).write_text(text, encoding="utf-8")

    def ev(seq, typ, **kw):
        e = {"trace_id": trace_id, "event_id": f"e{seq}", "sequence": seq,
             "type": typ, "status": "completed", "source": "system",
             "timestamp": "2026-01-01T00:00:00", **kw}
        db.execute(
            "INSERT INTO event_payloads(trace_id, sequence, type, timestamp, payload_json) "
            "VALUES(?,?,?,?,?)",
            (trace_id, seq, typ, "2026-01-01T00:00:00",
             json.dumps(e, ensure_ascii=False)),
        )

    ev(1, "run_start",
       input={"endpoint": "create", "thread_id": "t1", "session_name": "s1",
              "workspace_id": ws, "user_id": owner})
    seq = 2
    for ch_name in (chapters or {}):
        ev(seq, "tool_end", tool_name="write_file",
           agent_name="writing-subagent",
           tool_output={"content": f"Updated file /chapter/{ch_name}"})
        seq += 1
    ev(seq, "run_meta",
       input={"memory_quality": {"chapter_num": 1, "retrieval_ok": True,
                                 "evidence_nodes_count": 5, "evidence_edges_count": 8,
                                 "evidence_packet_tokens": 1200}})

    db.execute(
        "INSERT INTO nodes(node_id, trace_id, kind, agent_name, depth) VALUES(?,?,?,?,?)",
        ("n1", trace_id, "agent", "writing-subagent", 1),
    )


class DeliveryFreezeTest(unittest.TestCase):
    """B1：交付物冻结正文 + 指纹。"""

    def test_delivery_has_frozen_content_and_sha256(self):
        from app.dossier import extractor
        _seed_trace(chapters={"001.md": "第一章 觉醒\n\n" + "正文" * 50})
        try:
            facts = extractor.extract_facts("trace-b")
            deliveries = facts["deliveries"]
            self.assertIn("writing", deliveries)
            for path, meta in deliveries["writing"].items():
                # 结构：content_frozen / content_sha256 / char_count / truncated
                self.assertIn("content_frozen", meta)
                self.assertIn("content_sha256", meta)
                self.assertIn("char_count", meta)
                self.assertIn("truncated", meta)
                self.assertEqual(len(meta["content_sha256"]), 64)  # sha256 hex
                self.assertFalse(meta["truncated"])  # 短文本不截断
        finally:
            self._cleanup()

    def test_long_delivery_truncated_with_fingerprint(self):
        from app.dossier import extractor
        long_text = "长正文" * 5000  # 远超 8000 上限
        _seed_trace(trace_id="trace-long", chapters={"001.md": long_text})
        try:
            facts = extractor.extract_facts("trace-long")
            meta = next(iter(facts["deliveries"]["writing"].values()))
            self.assertTrue(meta["truncated"])
            self.assertLessEqual(len(meta["content_frozen"]),
                                 extractor.DELIVERY_FREEZE_CHAR_LIMIT)
            self.assertEqual(meta["char_count"], len(long_text))
        finally:
            self._cleanup("trace-long")

    def _cleanup(self, trace_id="trace-b"):
        db.execute("DELETE FROM event_payloads WHERE trace_id=?", (trace_id,))
        db.execute("DELETE FROM nodes WHERE trace_id=?", (trace_id,))
        db.execute("DELETE FROM runs WHERE trace_id=?", (trace_id,))


class MemoryQualityTest(unittest.TestCase):
    """B2：memory_quality 冻结进 facts。"""

    def test_memory_quality_in_facts(self):
        from app.dossier import extractor
        _seed_trace()
        try:
            facts = extractor.extract_facts("trace-b")
            mq = facts["memory_quality"]
            self.assertTrue(mq["available"])
            self.assertEqual(mq["summary"]["total_retrievals"], 1)
            self.assertEqual(mq["summary"]["ok_count"], 1)
            self.assertEqual(mq["summary"]["total_tokens"], 1200)
            self.assertEqual(len(mq["entries"]), 1)
            # evidence_id 格式 evt-{event_id}（event_id 由夹具动态生成）
            self.assertTrue(mq["entries"][0]["evidence_id"].startswith("evt-"))
            self.assertEqual(mq["entries"][0]["evidence_nodes_count"], 5)
        finally:
            db.execute("DELETE FROM event_payloads WHERE trace_id='trace-b'")
            db.execute("DELETE FROM nodes WHERE trace_id='trace-b'")
            db.execute("DELETE FROM runs WHERE trace_id='trace-b'")


class ContractCoverageMatrixTest(unittest.TestCase):
    """B3：任务契约驱动覆盖矩阵（确定性）。"""

    def test_no_contract_parse_yields_missing(self):
        """无契约语义提取（demand.md 缺失/LLM 不可用）→ 矩阵 missing。"""
        from app.dossier import compiler
        m = compiler._compute_contract_coverage_matrix(None, {"deliveries": {}, "review_chain": []})
        self.assertFalse(m["complete"])
        self.assertEqual(m["missing_count"], 1)

    def test_matrix_deterministic_overrides_agent_self_report(self):
        """需求 §33：即使 Agent 提取了契约，承诺产物未交付仍判 missing。"""
        from app.dossier import compiler
        contract_parsed = {
            "promised_artifacts": [
                {"kind": "chapter", "desc": "正文", "required": True},
                {"kind": "detail", "desc": "大纲", "required": False},
            ],
            "applicable_stages": ["interview", "writing"],
            "user_goal": "写小说",
            "hard_constraints": [{"constraint": "10万字"}],
            "style_preferences": None,
            "scope": "",
        }
        # writing 有交付，interview 无
        facts = {"deliveries": {"writing": {"/chapter/1.md": {}}}, "review_chain": []}
        m = compiler._compute_contract_coverage_matrix(contract_parsed, facts)
        # chapter covered（writing 交付）；interview missing；style/scope missing
        statuses = {i["key"]: i["status"] for i in m["items"]}
        self.assertEqual(statuses.get("chapter:正文"), "covered")
        self.assertEqual(statuses.get("interview"), "missing")
        self.assertEqual(statuses.get("style_preferences"), "missing")
        self.assertFalse(m["complete"])  # 有 missing 项

    def test_complete_when_all_applicable_covered(self):
        from app.dossier import compiler
        contract_parsed = {
            "promised_artifacts": [{"kind": "chapter", "desc": "正文", "required": True}],
            "applicable_stages": ["writing"],
            "user_goal": "写小说",
            "hard_constraints": [{"constraint": "10万字"}],
            "style_preferences": [{"pref": "严肃"}],
            "scope": "10万字以内",
        }
        facts = {"deliveries": {"writing": {"/chapter/1.md": {}}}, "review_chain": []}
        m = compiler._compute_contract_coverage_matrix(contract_parsed, facts)
        self.assertTrue(m["complete"])
        self.assertEqual(m["missing_count"], 0)


class DossierImmutabilityTest(unittest.TestCase):
    """B4：终态卷宗不可变。"""

    def setUp(self):
        db.execute(
            "INSERT INTO runs(trace_id, workspace_id, status, owner_user_id, run_purpose, "
            "ingested_at) VALUES(?,?,?,?,?,?)",
            ("trace-immut", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )

    def tearDown(self):
        db.execute("DELETE FROM evidence_dossiers WHERE trace_id='trace-immut'")
        db.execute("DELETE FROM runs WHERE trace_id='trace-immut'")

    def test_terminal_dossier_rejects_update(self):
        from app.dossier import repo
        from app.dossier.repo import DossierImmutableError
        did = repo.create_dossier("trace-immut", "u1", compile_rule_version="v1")
        repo.update_dossier(did, status="compiling")  # 合法
        repo.update_dossier(did, status="ready", manifest={"k": "v"}, finished=True)
        # 终态后再 update 应拒绝
        with self.assertRaises(DossierImmutableError):
            repo.update_dossier(did, status="partial", failure_reason="试图覆盖")
        with self.assertRaises(DossierImmutableError):
            repo.update_dossier(did, status="failed")

    def test_nonexistent_dossier_raises(self):
        from app.dossier.repo import DossierImmutableError
        from app.dossier import repo
        with self.assertRaises(DossierImmutableError):
            repo.update_dossier("nonexist", status="ready")

    def test_compile_flow_state_transitions_allowed(self):
        """编译流程内 pending→compiling→终态 是合法单次流程。"""
        from app.dossier import repo
        did = repo.create_dossier("trace-immut", "u1", compile_rule_version="v1")
        repo.update_dossier(did, status="compiling")
        repo.update_dossier(did, llm_calls_used=5)  # compiling 中更新资源消耗
        repo.update_dossier(did, status="ready", manifest={"k": "v"},
                            facts={"f": 1}, finished=True)
        d = repo.get_dossier(did)
        self.assertEqual(d["status"], "ready")


if __name__ == "__main__":
    unittest.main()
