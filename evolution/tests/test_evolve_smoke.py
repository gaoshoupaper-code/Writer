"""进化端冒烟单测（重构安全网，决策 D3 / 设计 S4）。

隔离策略（S4）：FastAPI TestClient + 临时 SQLite DB，只测校验逻辑——
evolve/eval_agent 的 start 端点在强前置校验阶段就 400 返回，不触及 LLM/executor。

覆盖：
  - import 冒烟：evolve/eval_agent/tests 全部模块可正常 import（重构后路径正确性）
  - evolve start 强前置校验：未评估的 trace 启动进化 → 400
  - evolve sessions 列表：空 DB 下返回空列表
  - eval_agent sessions 列表 + evaluated-traces 端点可调

设计依据：.claude/md/20260701_213000_进化端重构_设计.md
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 把 evolution/ 加入 sys.path（同 test_increment_reconstruct 模式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 在 import app 之前，把 DB 指向临时文件 ──────────────────────
# settings 是模块级单例，必须在 import app.core.settings 前注入环境变量。
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
# 测试不触发真实的 executor 轮询 / 活跃大盘轮询，禁用避免后台线程干扰
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

from fastapi.testclient import TestClient

import app.core.db as db
from app.core.settings import settings
from app.main import app

_old_db = settings.evolution_db


def setUpModule() -> None:
    """模块级初始化：重置 DB 连接 + 建表，确保用临时空库。"""
    settings.evolution_db = _tmp_db.name
    db._conn = None  # 重置单例连接，强制重连到临时库
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


class ImportSmokeTest(unittest.TestCase):
    """所有核心模块能正常 import（重构后导入路径正确性的守门员）。"""

    def test_import_evolve_modules(self) -> None:
        import app.evolve.api  # noqa: F401
        import app.evolve.ctx  # noqa: F401
        import app.evolve.db  # noqa: F401
        import app.evolve.docs  # noqa: F401
        import app.evolve.agent.agent  # noqa: F401
        import app.evolve.agent.middleware.flow_guard  # noqa: F401
        import app.evolve.agent.tools.flow  # noqa: F401
        import app.evolve.agent.tools.inspect  # noqa: F401

    def test_import_eval_agent_modules(self) -> None:
        import app.eval_agent.api  # noqa: F401
        import app.eval_agent.ctx  # noqa: F401
        import app.eval_agent.repo  # noqa: F401
        import app.eval_agent.tools  # noqa: F401
        import app.eval_agent.agent  # noqa: F401

    def test_import_tests_modules(self) -> None:
        import app.tests.api  # noqa: F401
        import app.tests.repo  # noqa: F401


class EvolveStartGuardTest(unittest.TestCase):
    """evolve start 端点的强前置校验（不触及 LLM/executor）。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_start_rejects_unknown_evaluation_dossier(self) -> None:
        """不存在的评估卷宗启动进化 → 409，且返回缺失事实。"""
        resp = self.client.post(
            "/api/evolve/start", json={"eval_dossier_id": "nonexistent-dossier"}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("dossier", resp.json()["detail"]["missing_fields"])

    def test_start_rejects_unsealed_evaluation_dossier(self) -> None:
        """未封存的评估卷宗启动进化 → 409，不启动下游 Trace。"""
        db.execute(
            """INSERT INTO evaluation_dossiers
               (dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version,
                trace_id, owner_user_id, completeness_status, seal_status, created_at)
               VALUES ('unsealed-dossier', 'eval-unsealed', 'evidence-1', 1,
                       'trace-source', 'user-1', 'incomplete', 'unsealed', '2026-01-01')"""
        )
        try:
            resp = self.client.post(
                "/api/evolve/start", json={"eval_dossier_id": "unsealed-dossier"}
            )
            self.assertEqual(resp.status_code, 409)
            missing = resp.json()["detail"]["missing_fields"]
            self.assertIn("seal_status=sealed", missing)
            self.assertIn("completeness_status=complete", missing)
        finally:
            db.execute("DELETE FROM evaluation_dossiers WHERE dossier_id='unsealed-dossier'")


class EvolveSessionsQueryTest(unittest.TestCase):
    """evolve sessions 查询端点（空库基线）。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_list_sessions_empty(self) -> None:
        """空 DB 下 sessions 列表返回空。"""
        resp = self.client.get("/api/evolve/sessions")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["sessions"], [])


class EvalAgentQueryTest(unittest.TestCase):
    """eval_agent 查询端点可调（不触及 LLM）。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_list_eval_sessions(self) -> None:
        resp = self.client.get("/api/eval-agent/sessions")
        self.assertEqual(resp.status_code, 200)

    def test_evaluated_traces(self) -> None:
        resp = self.client.get("/api/eval-agent/evaluated-traces")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
