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


def setUpModule() -> None:
    """模块级初始化：重置 DB 连接 + 建表，确保用临时空库。"""
    # settings 在 import 时已读 EVOLUTION_DB，确认指向临时文件
    db._conn = None  # 重置单例连接，强制重连到临时库
    db.init_db()


class ImportSmokeTest(unittest.TestCase):
    """所有核心模块能正常 import（重构后导入路径正确性的守门员）。"""

    def test_import_evolve_modules(self) -> None:
        import app.evolve.api  # noqa: F401
        import app.evolve.ctx  # noqa: F401
        import app.evolve.db  # noqa: F401
        import app.evolve.docs  # noqa: F401
        import app.evolve.driver.agent  # noqa: F401
        import app.evolve.driver.middleware.phase_guard  # noqa: F401
        import app.evolve.subagents.plan.build  # noqa: F401
        import app.evolve.subagents.execute.build  # noqa: F401

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

    def test_start_rejects_unknown_trace(self) -> None:
        """不存在的 trace 启动进化 → 404。"""
        resp = self.client.post(
            "/api/evolve/start", json={"trace_id": "nonexistent_trace"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_start_rejects_unevaluated_trace(self) -> None:
        """存在但未评估的 trace 启动进化 → 400（强前置：必须先评估）。"""
        # 先在 runs 表插一条 trace（让它通过 404 校验），但不建评估记录
        db.execute(
            """INSERT INTO runs (trace_id, workspace_id, status, ingested_at)
               VALUES (?, 'ws_test', 'completed', '2026-01-01T00:00:00Z')""",
            ("trace_no_eval",),
        )
        resp = self.client.post(
            "/api/evolve/start", json={"trace_id": "trace_no_eval"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("评估", resp.json()["detail"])


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
