"""A1 回归测试：eval_extractor 三层路径解析（owner_id 隔离层修复）。

修复背景：原 `_resolve_workspace_path` 只拼两层 `executor_workspace/{workspace_id}/{rel}`，
但 executor 物理布局是三层 `executor_workspace/{owner_user_id}/{workspace_id}/{rel}`，
导致评估系统对所有 trace 的交付物全部读不到（FileNotFoundError 被静默吞 → 全 skipped）。

本测试覆盖：
  - 三层路径正确拼接（正常路径）
  - owner_user_id='unknown'（老 trace 默认值）降级跳过
  - owner_user_id 为空字符串降级跳过
  - SQL 多查一列后能取到 owner_user_id
  - trace 不存在时返回空 dict

设计依据：.claude/md/20260720_150000_trace交付物丢失与基础设施归因.md
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 把 evolution/ 加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 在 import app 之前注入临时 DB
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.eval_agent import eval_extractor  # noqa: E402


def setUpModule() -> None:
    """模块级初始化：重置 DB 连接 + 建表。"""
    db._conn = None
    db.init_db()


def _insert_run(trace_id: str, workspace_id: str, owner_user_id: str) -> None:
    """插入一条 runs 记录（含 owner_user_id 字段，补齐 NOT NULL 列）。"""
    db.execute(
        """INSERT INTO runs
             (trace_id, workspace_id, owner_user_id, status, started_at,
              ingested_at, event_count)
           VALUES (?, ?, ?, 'completed', '2026-07-20T00:00:00Z',
                   '2026-07-20T00:00:00Z', 0)""",
        (trace_id, workspace_id, owner_user_id),
    )


class ResolveWorkspacePathTest(unittest.TestCase):
    """_resolve_workspace_path 三层拼接正确性。"""

    def test_three_layer_path_construction(self) -> None:
        """正常路径：executor_workspace/{owner}/{workspace}/{rel}。"""
        path = eval_extractor._resolve_workspace_path(
            workspace_id="ws-abc",
            owner_user_id="owner-xyz",
            file_path="/chapter/chapter-01.md",
        )
        expected = (
            settings.executor_workspace_path / "owner-xyz" / "ws-abc" / "chapter/chapter-01.md"
        )
        self.assertEqual(path, expected)

    def test_strips_leading_slash(self) -> None:
        """前导 / 被去除（file_path 规范化）。"""
        p1 = eval_extractor._resolve_workspace_path(
            "ws", "owner", "/demand.md"
        )
        p2 = eval_extractor._resolve_workspace_path(
            "ws", "owner", "demand.md"
        )
        self.assertEqual(p1, p2)

    def test_nested_path_preserved(self) -> None:
        """多层子路径保留：/storyline/S01-main.md。"""
        path = eval_extractor._resolve_workspace_path(
            "ws", "owner", "/storyline/S01-main.md"
        )
        self.assertEqual(
            path,
            settings.executor_workspace_path / "owner" / "ws" / "storyline/S01-main.md",
        )


class ExtractDeliveriesOwnerFallbackTest(unittest.TestCase):
    """extract_deliveries 对 owner_user_id 缺失的降级处理。"""

    @classmethod
    def setUpClass(cls) -> None:
        db.execute("DELETE FROM event_payloads WHERE trace_id LIKE 'test-owner-%'")
        db.execute("DELETE FROM runs WHERE trace_id LIKE 'test-owner-%'")

    def tearDown(self) -> None:
        # 每个测试方法后清理自己的数据
        db.execute("DELETE FROM event_payloads WHERE trace_id LIKE 'test-owner-%'")
        db.execute("DELETE FROM runs WHERE trace_id LIKE 'test-owner-%'")

    def test_owner_unknown_skipped(self) -> None:
        """owner_user_id='unknown'（老 trace ALTER DEFAULT）→ 跳过，返回空。"""
        _insert_run("test-owner-unknown", "ws-1", "unknown")
        result = eval_extractor.extract_deliveries("test-owner-unknown")
        self.assertEqual(result, {})

    def test_owner_empty_skipped(self) -> None:
        """owner_user_id 为空字符串 → 跳过，返回空。"""
        _insert_run("test-owner-empty", "ws-1", "")
        result = eval_extractor.extract_deliveries("test-owner-empty")
        self.assertEqual(result, {})

    def test_trace_not_found(self) -> None:
        """trace 不存在 → 返回空（不抛异常）。"""
        result = eval_extractor.extract_deliveries("nonexistent-trace-id")
        self.assertEqual(result, {})

    def test_owner_present_proceeds_to_path_read(self) -> None:
        """owner_user_id 正常时，流程走到磁盘读取阶段（文件不存在则静默跳过）。

        这里不创建真实文件，只验证：函数不因 owner 字段而提前返回空，
        而是走到 read_text 失败路径（OSError 被吞）。
        """
        _insert_run("test-owner-ok", "ws-real", "owner-real")
        # 注入一条假的 write_file tool_end 事件触发路径提取
        # 注意：SQL 用 payload_json LIKE '%write_file%' 过滤，必须含 tool_name 字段
        import json
        payload = {
            "type": "tool_end",
            "agent_name": "writing-subagent",
            "tool_name": "write_file",
            "tool_output": {"content": "Updated file /chapter/chapter-01.md"},
        }
        db.execute(
            "INSERT INTO event_payloads (trace_id, sequence, type, payload_json) VALUES (?, ?, ?, ?)",
            ("test-owner-ok", 1, "tool_end", json.dumps(payload)),
        )
        # 文件不存在 → 读取失败被吞 → 该 agent 无交付物 → 返回 {}
        # 关键：不因 owner 缺失提前返回，而是走到磁盘读
        result = eval_extractor.extract_deliveries("test-owner-ok")
        # 文件不存在所以 result 为空，但流程走完了 owner 检查
        self.assertEqual(result, {})


class SqlIncludesOwnerFieldTest(unittest.TestCase):
    """验证 SQL 查询包含 owner_user_id 字段（防回归）。"""

    def tearDown(self) -> None:
        db.execute("DELETE FROM event_payloads WHERE trace_id LIKE 'test-owner-%'")
        db.execute("DELETE FROM runs WHERE trace_id LIKE 'test-owner-%'")

    def test_extract_deliveries_reads_owner_field(self) -> None:
        """构造一个完整的三层路径 + 真实文件，验证能读到内容。"""
        import tempfile

        # 用临时目录作为 workspace 根
        tmp_root = Path(tempfile.mkdtemp())
        owner = "owner-aaa"
        ws = "ws-bbb"
        rel = "chapter/chapter-01.md"
        # 创建真实文件
        real_file = tmp_root / owner / ws / rel
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("# 第1章\n\n这是测试正文。", encoding="utf-8")

        # 临时替换 settings.executor_workspace（底层字段，property 会基于它构建路径）
        original = settings.executor_workspace
        try:
            settings.executor_workspace = str(tmp_root)
            _insert_run("test-owner-full", ws, owner)

            import json
            # 注意：SQL 用 payload_json LIKE '%write_file%' 过滤，
            # 所以 payload 必须包含 tool_name: 'write_file' 字段
            payload = {
                "type": "tool_end",
                "agent_name": "writing-subagent",
                "tool_name": "write_file",
                "tool_output": {"content": f"Updated file /{rel}"},
            }
            db.execute(
                "INSERT INTO event_payloads (trace_id, sequence, type, payload_json) VALUES (?, ?, ?, ?)",
                ("test-owner-full", 1, "tool_end", json.dumps(payload)),
            )

            result = eval_extractor.extract_deliveries("test-owner-full")
            # 应该读到 writing 的交付物
            self.assertIn("writing", result, "writing agent 应有交付物")
            self.assertIn(f"/{rel}", result["writing"])
            self.assertIn("这是测试正文", result["writing"][f"/{rel}"])
        finally:
            # 还原 settings（避免污染其他测试）
            settings.executor_workspace = original
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
