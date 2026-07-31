"""问题知识库一期契约测试（需求 20260731_135839，阶段 A+B）。

覆盖关键 AC：
  - AC-42 收录幂等（同 dossier+finding 不重复）
  - AC-32 多轴分类回钻（location 从 frozen_evidence 回钻，未知值未分类）
  - AC-41 空库生成新标准问题候选
  - AC-22 正式频率按独立 trace 数（同 trace 多 finding 只算 1）
  - AC-33 进化点一对一归属
  - AC-27 当前问题卡冻结（REQ-04.8 全字段）
  - AC-28 问题分组（同机制归组）
  - AC-06/30/31 检索注入（标注确认状态，无经验推荐）
  - AC-26 检索降级（无 embedder 时 FTS+LIKE 兜底）

设计依据：.claude/md/20260731_135839_进化问题知识库.md
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 在 import app 前，DB 指向临时文件 + 禁用 embedding（向量降级路径）
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"
os.environ["PROBLEM_KB_EMBED_API_KEY"] = ""  # 强制向量降级

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402

_old_db = settings.evolution_db


def setUpModule() -> None:
    settings.evolution_db = _tmp_db.name
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    settings.evolution_db = _old_db
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


class TestProblemInstanceIngest(unittest.TestCase):
    """AC-42/32/41：问题实例收录、分类、候选生成。"""

    def test_ingest_findings_creates_instances(self):
        from app.problem_kb import ingest, repo
        conn = db.get_conn()
        findings = [
            {"id": "f01", "dimension": "协作拓扑", "severity": "high",
             "finding": "Agent调用超时", "evidence": "timeout",
             "evidence_ref": ["evt-1"], "evidence_type": "实证"},
            {"id": "f02", "dimension": "内容质量", "severity": "low",
             "finding": "文风冗余", "evidence": "重复", "evidence_type": "推断"},
        ]
        frozen = {"evt-1": {"agent_name": "writer", "type": "tool_call", "sequence": 3}}
        n = ingest.ingest_findings_on_seal(
            dossier_id="d1", trace_id="t1", findings=findings,
            frozen_evidence=frozen, conn=conn)
        conn.commit()
        self.assertEqual(n, 2)

    def test_ingest_idempotent_ac42(self):
        """AC-42：重复收录不产生重复实例。"""
        from app.problem_kb import ingest, repo
        conn = db.get_conn()
        findings = [{"id": "f01", "dimension": "协作拓扑", "severity": "high",
                     "finding": "超时", "evidence": "e", "evidence_ref": ["e1"]}]
        ingest.ingest_findings_on_seal(dossier_id="d2", trace_id="t2",
                                       findings=findings, frozen_evidence={}, conn=conn)
        conn.commit()
        # 重复
        n = ingest.ingest_findings_on_seal(dossier_id="d2", trace_id="t2",
                                           findings=findings, frozen_evidence={}, conn=conn)
        conn.commit()
        self.assertEqual(n, 0)  # 幂等
        instances = repo.list_by_dossier("d2")
        self.assertEqual(len(instances), 1)

    def test_classification_drills_back_ac32(self):
        """AC-32：location 从 frozen_evidence 回钻，未知值未分类。"""
        from app.problem_kb.classifier import classify_finding
        from app.problem_kb.taxonomy import UNCLASSIFIED
        finding = {"id": "f01", "dimension": "协作拓扑", "severity": "high",
                   "finding": "调用超时", "evidence": "timeout",
                   "evidence_ref": ["evt-1"]}
        frozen = {"evt-1": {"agent_name": "writer", "type": "tool_call", "sequence": 5}}
        cls = classify_finding(finding, frozen)
        self.assertEqual(cls["location"]["agent"], "writer")
        self.assertEqual(cls["location"]["component"], "tool_call")
        self.assertEqual(cls["affected_mechanism"], "协作拓扑")
        self.assertEqual(cls["failure_nature"], "超时")

    def test_classification_unknown_is_unclassified_ac32(self):
        """AC-32：提取不到的轴记为未分类，不静默创造类别。"""
        from app.problem_kb.classifier import classify_finding
        from app.problem_kb.taxonomy import UNCLASSIFIED
        finding = {"id": "f01", "dimension": "未知维度", "severity": "low",
                   "finding": "含糊问题", "evidence": "无关键词"}
        cls = classify_finding(finding, {})
        self.assertEqual(cls["affected_mechanism"], UNCLASSIFIED)
        self.assertEqual(cls["location"]["agent"], UNCLASSIFIED)

    def test_empty_lib_generates_new_problem_candidate_ac41(self):
        """AC-41：空库收录中高严重度实例 → 生成新标准问题候选。"""
        from app.problem_kb import ingest, repo
        conn = db.get_conn()
        ingest.ingest_findings_on_seal(
            dossier_id="d-ac41", trace_id="t-ac41",
            findings=[{"id": "f01", "dimension": "协作拓扑", "severity": "high",
                       "finding": "全新问题", "evidence": "e", "evidence_ref": ["e1"]}],
            frozen_evidence={}, conn=conn)
        conn.commit()
        pending = repo.list_pending()
        new_proposals = [c for c in pending if c["is_new_problem_proposal"]]
        self.assertGreaterEqual(len(new_proposals), 1)


class TestFormalFrequency(unittest.TestCase):
    """AC-22：正式频率按已确认独立 trace 数计算。"""

    def test_same_trace_multiple_findings_counts_once_ac22(self):
        """同一 trace 多条 finding 归同一标准问题 → 正式频率只 +1。"""
        from app.problem_kb import repo
        pid = repo.create_problem(title="P-ac22")
        # 同 trace 两个实例（用唯一标识避免与其他测试数据冲突）
        i1 = repo.create_instance(dossier_id="d-ac22-a", trace_id="trace-ac22-same",
                                  finding_id="f01", severity="high", statement="a")
        i2 = repo.create_instance(dossier_id="d-ac22-a", trace_id="trace-ac22-same",
                                  finding_id="f02", severity="high", statement="b")
        repo.link_instance(instance_id=i1, problem_id=pid, confirmed_by="u")
        repo.link_instance(instance_id=i2, problem_id=pid, confirmed_by="u")
        freq = repo.recalc_formal_frequency(pid)
        self.assertEqual(freq, 1)  # 同 trace 只算 1

        # 另一独立 trace
        i3 = repo.create_instance(dossier_id="d-ac22-b", trace_id="trace-ac22-other",
                                  finding_id="f01", severity="high", statement="c")
        repo.link_instance(instance_id=i3, problem_id=pid, confirmed_by="u")
        freq2 = repo.recalc_formal_frequency(pid)
        self.assertEqual(freq2, 2)


class TestEvolutionPointOwnership(unittest.TestCase):
    """AC-33：进化点一对一归属。"""

    def setUp(self):
        db.execute(
            "INSERT INTO evolve_sessions(session_id, case_id, status, created_at, updated_at, "
            "bound_eval_dossier_id) VALUES(?,?,?,?,?,?)",
            ("sess-own", "", "created", "2026-01-01", "2026-01-01", "d-own"))

    def test_ownership_assigns_from_finding_ref_ac33(self):
        """propose 引用 f01 → 归属解析到对应实例。"""
        from app.problem_kb import repo
        from app.evolve.evolve_repo import _try_assign_point_ownership
        # 收录实例
        conn = db.get_conn()
        repo.create_instance(dossier_id="d-own", trace_id="t-own",
                             finding_id="f01", severity="high", statement="x", conn=conn)
        conn.commit()
        _try_assign_point_ownership("pt-own1", "sess-own",
                                    "评估 finding f01 指出问题")
        ownership = repo.get_ownership("pt-own1")
        self.assertIsNotNone(ownership)
        self.assertIsNotNone(ownership["source_instance_id"])

    def test_ownership_one_to_one_ac33(self):
        """一个进化点重复归属不覆盖首次（UNIQUE 强制一对一）。"""
        from app.problem_kb import repo
        repo.assign_ownership(point_id="pt-dupe", problem_id="p1", source_instance_id="i1")
        repo.assign_ownership(point_id="pt-dupe", problem_id="p2", source_instance_id="i2")
        ownership = repo.get_ownership("pt-dupe")
        self.assertEqual(ownership["problem_id"], "p1")  # 保持首次


class TestCurrentProblemCard(unittest.TestCase):
    """AC-27/28：当前问题卡冻结 + 分组。"""

    def test_card_freezes_all_required_fields_ac27(self):
        """REQ-04.8：冻结的快照含全部必填字段。"""
        from app.problem_kb import current_card
        dossier = {
            "dossier_id": "d-card", "trace_id": "t-card",
            "findings": [{"id": "f01", "dimension": "协作拓扑", "severity": "high",
                          "finding": "问题陈述", "evidence": "证据",
                          "evidence_ref": ["evt-1"], "evidence_type": "实证"}],
            "frozen_evidence": {"evt-1": {"agent_name": "w", "type": "c", "sequence": 1}},
        }
        cards = current_card.freeze_current_cards("sess-card", dossier)
        self.assertEqual(len(cards), 1)
        snap = cards[0]["frozen_snapshot"]
        for field in ("statement", "direct_evidence", "symptom", "severity",
                      "root_cause_hypothesis", "root_cause_confidence",
                      "alternative_explanations", "unknowns"):
            self.assertIn(field, snap, f"快照缺字段 {field}")

    def test_cards_grouped_by_mechanism_ac28(self):
        """AC-28：同受影响机制的卡归同一 problem_group。"""
        from app.problem_kb import current_card
        dossier = {
            "dossier_id": "d-grp", "trace_id": "t-grp",
            "findings": [
                {"id": "f01", "dimension": "协作拓扑", "severity": "high",
                 "finding": "超时问题1", "evidence": "e", "evidence_ref": ["e1"]},
                {"id": "f02", "dimension": "协作拓扑", "severity": "medium",
                 "finding": "超时问题2", "evidence": "e", "evidence_ref": ["e2"]},
                {"id": "f03", "dimension": "内容质量", "severity": "low",
                 "finding": "冗余问题", "evidence": "e", "evidence_ref": ["e3"]},
            ],
            "frozen_evidence": {},
        }
        cards = current_card.freeze_current_cards("sess-grp", dossier)
        groups = {c["problem_group"] for c in cards}
        # f01/f02 同机制同性质归一组，f03 不同机制
        self.assertEqual(len(groups), 2)


class TestRetrievalAndInjection(unittest.TestCase):
    """AC-06/26/30/31：检索 + 注入。"""

    def test_search_returns_confirmed_status_ac06(self):
        """AC-06：检索结果标注确认状态。"""
        from app.problem_kb import repo
        from app.problem_kb.retrieval import store, search
        pid = repo.create_problem(title="检索测试问题", description="用于检索验证")
        store.sync_problem_to_index(pid, "检索测试问题", "用于检索验证", "测试")
        result = search.search_similar_problems(
            query_text="检索测试", query_vec=None, top_k=5)
        self.assertGreater(len(result.hits), 0)
        self.assertEqual(result.hits[0]["confirmation_status"], "已确认标准问题")
        self.assertIn("effect_stage", result.hits[0])

    def test_injection_no_experience_recommendation_ac30(self):
        """AC-30/31：注入文本不含经验对象/等级/推荐。"""
        from app.problem_kb import repo
        from app.problem_kb.retrieval import store
        from app.evolve.ctx import EvolveContext
        from app.evolve.agent.agent import _format_similar_trajectories
        pid = repo.create_problem(title="注入测试", description="验证无经验推荐")
        store.sync_problem_to_index(pid, "注入测试", "验证无经验推荐", "测试")
        ctx = EvolveContext(session_id="sess-inj")
        ctx.eval_dossier = {
            "dossier_id": "d-inj", "trace_id": "t-inj",
            "findings": [{"id": "f01", "dimension": "内容质量", "severity": "high",
                          "finding": "注入测试问题", "evidence": "e",
                          "evidence_ref": ["e1"], "evidence_type": "实证"}],
            "frozen_evidence": {"e1": {"agent_name": "w"}},
        }
        text = _format_similar_trajectories(ctx)
        # 不含经验对象/等级/推荐
        for forbidden in ("经验等级", "建议采用", "经验推荐", "可复用经验"):
            self.assertNotIn(forbidden, text, f"注入文本不应含 '{forbidden}'")

    def test_degraded_retrieval_does_not_claim_no_history_ac26(self):
        """AC-26：检索无结果时不表述为'无历史问题'。"""
        from app.evolve.ctx import EvolveContext
        from app.evolve.agent.agent import _format_similar_trajectories
        ctx = EvolveContext(session_id="sess-empty")
        ctx.eval_dossier = {
            "dossier_id": "d-empty", "trace_id": "t-empty",
            "findings": [{"id": "f01", "dimension": "内容质量", "severity": "high",
                          "finding": "完全无匹配的冷门问题xyz", "evidence": "e",
                          "evidence_ref": ["e1"], "evidence_type": "实证"}],
            "frozen_evidence": {"e1": {"agent_name": "w"}},
        }
        text = _format_similar_trajectories(ctx)
        self.assertNotIn("没有历史问题", text)
        self.assertNotIn("无历史问题", text)


if __name__ == "__main__":
    unittest.main()
