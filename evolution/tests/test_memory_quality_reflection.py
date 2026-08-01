"""进化反思从 A/B trace 消费记忆信号测试（REQ-20260801-131224 AC-005）。

覆盖：
  - AC-005：reflection 能从 A/B trace 的 memory_quality run_meta 事件归纳记忆失败模式
    （recall_miss / retrieval_fail），不再因 0 条事件返回空。第 1 章空召回不误报。

设计依据：.claude/md/20260801_131224_AB测试路径接入记忆系统.md
          evolution/app/reflection/extractor.py:extract_from_memory_quality
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.reflection import extractor
from app.core import db as db_mod


class MemoryQualityReflectionTest(unittest.TestCase):
    """AC-005：extract_from_memory_quality 归纳记忆失败模式。"""

    def setUp(self) -> None:
        # 用独立内存 SQLite 替换全局连接，避免污染真实库。
        self._tmp = tempfile.TemporaryDirectory()
        test_db = Path(self._tmp.name) / "test.db"
        self._conn = sqlite3.connect(str(test_db), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._prev_conn = db_mod._conn
        db_mod._conn = self._conn
        # 建测试需要的表（event_payloads + reflection_library）。
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_payloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                sequence INTEGER,
                type TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reflection_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                pattern TEXT NOT NULL,
                symptom TEXT DEFAULT '',
                suggestion TEXT DEFAULT '',
                source_traces TEXT DEFAULT '[]',
                hit_count INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        self._conn.commit()

    def tearDown(self) -> None:
        db_mod._conn = self._prev_conn
        self._conn.close()
        self._tmp.cleanup()

    def _insert_event(self, trace_id: str, seq: int, payload: dict) -> None:
        """插入一条 run_meta 事件（payload 含 input.memory_quality）。"""
        self._conn.execute(
            "INSERT INTO event_payloads (trace_id, sequence, type, payload_json) VALUES (?,?,?,?)",
            (trace_id, seq, "run_meta", json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.commit()

    def _mq_event(self, chapter: int, ok: bool, nodes: int, edges: int, error: str | None = None) -> dict:
        """构造一条含 input.memory_quality 的 run_meta 事件 payload。"""
        return {
            "type": "run_meta",
            "source": "middleware",
            "agent_name": "writing",
            "input": {
                "memory_quality": {
                    "chapter_num": chapter,
                    "query": "写第{}章".format(chapter),
                    "retrieval_ok": ok,
                    "error": error,
                    "evidence_nodes_count": nodes,
                    "evidence_edges_count": edges,
                }
            },
        }

    def test_induces_recall_miss_when_empty_recall_after_chapter_1(self) -> None:
        """召回为空（0 节点 0 边）且非第 1 章 → 归纳 recall_miss（AC-001/EDGE-002）。

        Given：A/B trace 含第 3 章 memory_quality 事件，nodes=0 edges=0。
        When：reflection 运行 extract_from_memory_quality。
        Then：归纳 1 条 recall_miss，不再返回 0。
        """
        trace_id = "trace-ab-mem-001"
        self._insert_event(trace_id, 1, self._mq_event(chapter=3, ok=True, nodes=0, edges=0))

        count = extractor.extract_from_memory_quality(trace_id)

        self.assertEqual(count, 1, "召回为空（非第1章）应归纳 1 条 recall_miss")
        rows = db_mod.query_all("SELECT category FROM reflection_library WHERE source_traces LIKE ?", (f"%{trace_id}%",))
        categories = [r["category"] for r in rows]
        self.assertIn("recall_miss", categories, "应写入 recall_miss 反思条目")

    def test_induces_retrieval_fail_when_retrieval_ok_false(self) -> None:
        """retrieval_ok=False（检索异常）→ 归纳 retrieval_fail（AC-006/NFR-001）。

        Given：A/B trace 含第 2 章 memory_quality 事件，retrieval_ok=False。
        When：reflection 运行 extract_from_memory_quality。
        Then：归纳 1 条 retrieval_fail。
        """
        trace_id = "trace-ab-mem-002"
        self._insert_event(trace_id, 1, self._mq_event(chapter=2, ok=False, nodes=0, edges=0, error="vec query timeout"))

        count = extractor.extract_from_memory_quality(trace_id)

        self.assertEqual(count, 1, "检索异常应归纳 1 条 retrieval_fail")
        rows = db_mod.query_all("SELECT category FROM reflection_library WHERE source_traces LIKE ?", (f"%{trace_id}%",))
        categories = [r["category"] for r in rows]
        self.assertIn("retrieval_fail", categories, "应写入 retrieval_fail 反思条目")

    def test_no_false_positive_for_chapter_1_empty_recall(self) -> None:
        """第 1 章召回为空属正常，不归纳 recall_miss（EDGE-002 不误报）。

        Given：A/B trace 含第 1 章 memory_quality 事件，nodes=0 edges=0（第1章记忆库为空）。
        When：reflection 运行 extract_from_memory_quality。
        Then：返回 0（第 1 章空召回是预期，非失败），不写入反思。
        """
        trace_id = "trace-ab-mem-003"
        self._insert_event(trace_id, 1, self._mq_event(chapter=1, ok=True, nodes=0, edges=0))

        count = extractor.extract_from_memory_quality(trace_id)

        self.assertEqual(count, 0, "第 1 章空召回是预期（记忆库为空），不应归纳 recall_miss（EDGE-002）")
        rows = db_mod.query_all("SELECT * FROM reflection_library WHERE source_traces LIKE ?", (f"%{trace_id}%",))
        self.assertEqual(rows, [], "第 1 章空召回不应写入任何反思条目")

    def test_returns_zero_when_no_memory_quality_events(self) -> None:
        """trace 无 memory_quality 事件时返回 0，不误报（FR-005 失败语义）。

        对比接入前：trace-90b897 全量 0 条 memory 事件，reflection 返回 0。
        接入后若有信号才归纳，无信号不误报。
        """
        trace_id = "trace-no-mem-004"
        # 插入一条非 memory_quality 的 run_meta 事件。
        self._insert_event(trace_id, 1, {
            "type": "run_meta",
            "input": {"contract_snapshot": {"task_type": "screenplay.ab_run"}},
        })

        count = extractor.extract_from_memory_quality(trace_id)

        self.assertEqual(count, 0, "无 memory_quality 事件应返回 0，不误报")

    def test_multiple_failures_induce_multiple_reflections(self) -> None:
        """一条 trace 含多个失败章节 → 归纳多条反思（recall_miss + retrieval_fail 混合）。"""
        trace_id = "trace-ab-mem-005"
        # 第 2 章召回空（recall_miss），第 3 章检索异常（retrieval_fail），第 4 章正常（不归纳）。
        self._insert_event(trace_id, 1, self._mq_event(chapter=2, ok=True, nodes=0, edges=0))
        self._insert_event(trace_id, 2, self._mq_event(chapter=3, ok=False, nodes=0, edges=0, error="db locked"))
        self._insert_event(trace_id, 3, self._mq_event(chapter=4, ok=True, nodes=5, edges=3))

        count = extractor.extract_from_memory_quality(trace_id)

        self.assertEqual(count, 2, "2 个失败章节（1 recall_miss + 1 retrieval_fail），正常章节不归纳")


if __name__ == "__main__":
    unittest.main()
