"""证据卷宗重命名契约测试（2026-07-27，证据分级可见性重构阶段 A）。

验证 evidence → dossier 全链路重命名后的关键不变量，防止旁路/旧路径回退：
  1. DB 迁移幂等：旧库 evidence_packs rename 成 evidence_dossiers，数据保留；
     已迁移库再次 init_db 无副作用。
  2. dossier repo 对外 dossier_id alias：DB 列名 pack_id，上层统一 dossier_id。
  3. evaluation_dossiers 表 + evaluation_sessions 尝试字段就位。
  4. 评估/进化 API 路由已迁移到 /api/dossier，无 /api/evidence 残留。
  5. evaluation_sessions 单卷宗单活动任务查询（需求 §40）。

设计依据：.claude/md/20260727_174943_进化证据分级可见性重构.md
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 在 import app 前，DB 指向临时文件 + 禁用 executor 轮询
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import sqlite3  # noqa: E402

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.main import app  # noqa: E402

_old_db = settings.evolution_db


def setUpModule() -> None:
    """模块级初始化：重置连接 + 建表。"""
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


class DbMigrationContractTest(unittest.TestCase):
    """DB 迁移幂等性 + 数据保留。"""

    def test_evidence_dossiers_table_exists_no_legacy(self):
        """新库直接建 evidence_dossiers，无 evidence_packs 残留。"""
        conn = sqlite3.connect(_tmp_db.name)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            self.assertIn("evidence_dossiers", tables)
            self.assertNotIn("evidence_packs", tables)
        finally:
            conn.close()

    def test_evidence_dossiers_column_is_pack_id(self):
        """DB 列名沿用 pack_id（rename 不改列名），代码层 alias 为 dossier_id。"""
        conn = sqlite3.connect(_tmp_db.name)
        try:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(evidence_dossiers)"
            ).fetchall()]
            self.assertIn("pack_id", cols)
            self.assertNotIn("dossier_id", cols)  # DB 层无此列
        finally:
            conn.close()

    def test_init_db_idempotent(self):
        """已迁移库再次 init_db 不改变表集合、不丢列。"""
        before = {r["name"] for r in db.query_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        db.init_db()  # 再跑一次
        after = {r["name"] for r in db.query_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertEqual(before, after)

    def test_rename_preserves_data(self):
        """旧库 evidence_packs（含数据）经迁移后数据完整保留在 evidence_dossiers。"""
        # 用独立临时库模拟旧库
        legacy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        legacy.close()
        conn = sqlite3.connect(legacy.name)
        conn.executescript("""
            CREATE TABLE evidence_packs (
                pack_id TEXT PRIMARY KEY, trace_id TEXT, owner_user_id TEXT,
                version INTEGER, is_current INTEGER, status TEXT, provenance TEXT,
                compile_rule_version TEXT, manifest_json TEXT, facts_json TEXT,
                semantic_json TEXT, index_json TEXT, eval_view_json TEXT,
                evolve_view_json TEXT, failure_reason TEXT, llm_calls_used INTEGER,
                created_at TEXT, finished_at TEXT, UNIQUE(trace_id, version)
            );
            INSERT INTO evidence_packs(pack_id, trace_id, status) VALUES
              ('leg-1','t1','ready'),('leg-2','t1','partial');
            CREATE INDEX idx_epk_trace ON evidence_packs(trace_id);
        """)
        conn.commit()
        # 跑 rename 迁移
        db._migrate_rename_evidence_packs_to_dossiers(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("evidence_dossiers", tables)
        self.assertNotIn("evidence_packs", tables)
        rows = conn.execute(
            "SELECT pack_id, status FROM evidence_dossiers ORDER BY pack_id"
        ).fetchall()
        self.assertEqual(rows, [("leg-1", "ready"), ("leg-2", "partial")])
        # 幂等
        db._migrate_rename_evidence_packs_to_dossiers(conn)
        conn.close()
        os.unlink(legacy.name)


class EvaluationAttemptContractTest(unittest.TestCase):
    """evaluation_sessions 演变为评估尝试 + evaluation_dossiers 表。"""

    def test_evaluation_dossiers_table_exists(self):
        conn = sqlite3.connect(_tmp_db.name)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            self.assertIn("evaluation_dossiers", tables)
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(evaluation_dossiers)"
            ).fetchall()]
            for need in ("dossier_id", "eval_attempt_id", "source_dossier_id",
                         "source_dossier_version", "conclusions_json", "findings_json",
                         "positive_patterns_json", "completeness_status", "seal_status"):
                self.assertIn(need, cols)
        finally:
            conn.close()

    def test_evaluation_sessions_attempt_columns(self):
        """evaluation_sessions 新增尝试字段（bound_dossier/资源/封存回填/原因）。"""
        conn = sqlite3.connect(_tmp_db.name)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(evaluation_sessions)"
            ).fetchall()}
            for need in ("bound_dossier_id", "sealed_dossier_id",
                         "model_calls_used", "tokens_used", "runtime_ms",
                         "failure_reason", "stop_reason"):
                self.assertIn(need, cols, f"evaluation_sessions 缺列 {need}")
        finally:
            conn.close()

    def test_single_active_attempt_per_dossier(self):
        """需求 §40：同一证据卷宗最多一个活动评估任务（复用而非并行）。"""
        from app.eval_agent import repo as eval_repo
        # 准备 runs + 卷宗
        db.execute(
            "INSERT INTO runs(trace_id, workspace_id, status, owner_user_id, run_purpose, ingested_at) "
            "VALUES(?,?,?,?,?,?)",
            ("trace-a", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )
        eval_repo.create_session("ev-1", "trace-a", bound_dossier_id="dos-a")
        # 已有活动任务 → 查得到
        active = eval_repo.get_active_attempt_by_dossier("dos-a")
        self.assertIsNotNone(active)
        self.assertEqual(active["eval_id"], "ev-1")
        # 完成它
        eval_repo.update_session("ev-1", status="completed", sealed_dossier_id="evd-1")
        self.assertIsNone(eval_repo.get_active_attempt_by_dossier("dos-a"))
        # 列尝试历史
        attempts = eval_repo.list_attempts_by_dossier("dos-a")
        self.assertEqual(len(attempts), 1)
        # 按封存卷宗反查
        self.assertEqual(
            eval_repo.get_attempt_by_sealed_dossier("evd-1")["eval_id"], "ev-1"
        )


class DossierRepoAliasTest(unittest.TestCase):
    """dossier repo dossier_id alias + 全链路。"""

    def setUp(self):
        from app.dossier import repo as dossier_repo
        self.repo = dossier_repo
        db.execute(
            "INSERT OR IGNORE INTO runs(trace_id, workspace_id, status, owner_user_id, run_purpose, ingested_at) "
            "VALUES(?,?,?,?,?,?)",
            ("trace-r", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )

    def test_dossier_id_alias_roundtrip(self):
        did = self.repo.create_dossier("trace-r", "u1", compile_rule_version="v1")
        self.repo.update_dossier(did, status="ready",
                                 manifest={"k": "v"}, facts={"f": 1}, finished=True)
        self.repo.mark_current(did)
        d = self.repo.get_dossier(did)
        self.assertEqual(d["dossier_id"], did)  # alias
        self.assertEqual(d["pack_id"], did)     # 原始列名仍可访问
        self.assertEqual(d["manifest"], {"k": "v"})
        self.assertEqual(self.repo.get_consumable_dossier("trace-r")["dossier_id"], did)
        self.assertEqual(len(self.repo.list_dossiers("trace-r")), 1)
        self.assertEqual(self.repo.delete_by_trace("trace-r"), 1)
        self.assertIsNone(self.repo.get_dossier(did))


class ApiRouteContractTest(unittest.TestCase):
    """API 路由迁移契约：/api/dossier 就位，/api/evidence 无残留。"""

    def test_dossier_routes_registered(self):
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertIn("/api/dossier/start", paths)
        self.assertIn("/api/dossier/sessions/{dossier_id}", paths)
        self.assertIn("/api/dossier/sessions/{dossier_id}/stop", paths)
        self.assertIn("/api/dossier/traces/{trace_id}/packs", paths)
        self.assertIn("/api/dossier/traces/{trace_id}/current", paths)
        self.assertIn("/api/dossier/packs/{dossier_id}/drill/{evidence_id}", paths)

    def test_no_evidence_routes_remain(self):
        """重命名后不应有任何 /api/evidence 路由残留。"""
        paths = {getattr(r, "path", "") for r in app.routes}
        legacy = [p for p in paths if "/api/evidence" in p]
        self.assertEqual(legacy, [], f"残留旧路由: {legacy}")


if __name__ == "__main__":
    unittest.main()
