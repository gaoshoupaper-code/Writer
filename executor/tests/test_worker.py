"""Phase 2 T2.1：worker 服务测试（重点测 harness 动态加载）。

动态加载（load_harness_instance）是 D4 proposer 代码进生产的关键路径——
proposer 生成的 harness.py 要能被 worker 正确加载为 WriterHarness 实例。

覆盖：
- 正常加载（合法 harness.py → 实例）
- 文件不存在 → HarnessLoadError
- 语法错 → HarnessLoadError
- 无 WriterHarness 子类 → HarnessLoadError
- health 端点返回 harness_id
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.worker.server import (
    HarnessLoadError,
    create_worker_app,
    load_harness_instance,
)


# ── 动态加载测试 ────────────────────────────────────────────


_VALID_HARNESS_CODE = '''
from app.platform.harness import WriterHarness, HarnessContext


class MyTestHarness(WriterHarness):
    def build_system_prompt(self, ctx):
        return "test prompt"
    def build_skills(self, ctx):
        return []
    def build_middleware(self, ctx):
        return []
    def build_subagents(self, ctx):
        return []
'''


class TestLoadHarnessInstance:
    def test_load_valid_harness(self, tmp_path) -> None:
        """合法 harness.py → 加载成功，返回 WriterHarness 实例。"""
        version_dir = tmp_path / "1"
        version_dir.mkdir()
        code_path = version_dir / "harness.py"
        code_path.write_text(_VALID_HARNESS_CODE, encoding="utf-8")

        instance = load_harness_instance(code_path)
        from app.platform.harness import WriterHarness
        assert isinstance(instance, WriterHarness)
        assert instance.harness_id() == "MyTestHarness"

    def test_load_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(HarnessLoadError, match="不存在"):
            load_harness_instance(tmp_path / "nonexistent.py")

    def test_load_syntax_error_raises(self, tmp_path) -> None:
        version_dir = tmp_path / "2"
        version_dir.mkdir()
        code_path = version_dir / "harness.py"
        code_path.write_text("def broken(:\n", encoding="utf-8")  # 语法错

        with pytest.raises(HarnessLoadError, match="执行失败"):
            load_harness_instance(code_path)

    def test_load_no_harness_subclass_raises(self, tmp_path) -> None:
        """文件无 WriterHarness 子类 → HarnessLoadError。"""
        version_dir = tmp_path / "3"
        version_dir.mkdir()
        code_path = version_dir / "harness.py"
        code_path.write_text(
            "class NotAHarness:\n    pass\n", encoding="utf-8"
        )

        with pytest.raises(HarnessLoadError, match="未定义 WriterHarness 子类"):
            load_harness_instance(code_path)

    def test_load_multiple_subclasses_takes_last(self, tmp_path) -> None:
        """多个子类时取最后定义的。"""
        version_dir = tmp_path / "4"
        version_dir.mkdir()
        code_path = version_dir / "harness.py"
        code_path.write_text(
            _VALID_HARNESS_CODE.replace("MyTestHarness", "FirstHarness")
            + "\n\nclass SecondHarness(WriterHarness):\n"
            "    def build_system_prompt(self, ctx): return ''\n"
            "    def build_skills(self, ctx): return []\n"
            "    def build_middleware(self, ctx): return []\n"
            "    def build_subagents(self, ctx): return []\n",
            encoding="utf-8",
        )
        instance = load_harness_instance(code_path)
        assert instance.harness_id() == "SecondHarness"


# ── worker app 测试 ─────────────────────────────────────────


class TestWorkerApp:
    def test_health_endpoint(self, tmp_path) -> None:
        """health 端点返回 harness_id。"""
        # 先加载一个合法 harness
        version_dir = tmp_path / "1"
        version_dir.mkdir()
        (version_dir / "harness.py").write_text(_VALID_HARNESS_CODE, encoding="utf-8")
        instance = load_harness_instance(version_dir / "harness.py")

        app = create_worker_app(instance)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["harness_id"] == "MyTestHarness"

    def test_generate_stream_returns_501_pending(self, tmp_path) -> None:
        """生成端点待接入（T2.2），返回 501。"""
        version_dir = tmp_path / "1"
        version_dir.mkdir()
        (version_dir / "harness.py").write_text(_VALID_HARNESS_CODE, encoding="utf-8")
        instance = load_harness_instance(version_dir / "harness.py")

        app = create_worker_app(instance)
        client = TestClient(app)
        resp = client.post("/generate/stream", json={
            "workspace_path": "/tmp/ws",
            "payload": {"premise": "test"},
        })
        assert resp.status_code == 501
