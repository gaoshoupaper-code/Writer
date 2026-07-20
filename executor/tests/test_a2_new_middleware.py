"""A2 加固中间件单元测试（R6 要求）。

覆盖 4 个新装配的中间件：
  - WriteResultInspectorMiddleware（A2-D3 新增）
  - EncodingGuardMiddleware（A2-D1 加截断检测 + 去 latin-1 洗白 + 改返回 error ToolMessage）
  - ReadCacheMiddleware（A2-D2 加写后失效钩子）
  - FileStateTrackerMiddleware（A2-D4 修剪死代码后）

测试加载方式：直接从 evolution/harnesses/repo/ 源仓库加载（harness_current 模式），
因为 production checkout 停在 v2 还没同步到最新——这是 A2 修复本身的根因之一。
等 A2 部署上线后 production 同步，这些测试自动覆盖到 production 版本。

设计依据：.claude/md/20260720_150000_trace交付物丢失与基础设施归因.md
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage

# ── 加载 harness 源仓库作为 package ──
_REPO_DIR = Path(__file__).resolve().parent.parent.parent / "evolution" / "harnesses" / "repo"

_PKG_NAME = "_harness_test_pkg"
if _PKG_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _REPO_DIR / "__init__.py",
        submodule_search_locations=[str(_REPO_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = pkg
    spec.loader.exec_module(pkg)

# 现在可以从 _PKG_NAME 导入子模块
from _harness_test_pkg.middleware.write_result_inspector import (  # noqa: E402
    WriteFailedError,
    WriteResultInspectorMiddleware,
)
from _harness_test_pkg.middleware.encoding_guard import EncodingGuardMiddleware  # noqa: E402
from _harness_test_pkg.middleware.read_cache import ReadCacheMiddleware  # noqa: E402
from _harness_test_pkg.middleware.file_state_tracker import (  # noqa: E402
    FileStateTrackerMiddleware,
)


# ── 公共工具：构造伪 request（对齐 test_meta_readonly_middleware 模式）──


def _request(
    tool_name: str,
    *,
    file_path: str = "",
    content: str = "",
    old_string: str = "",
    tool_call_id: str = "call-1",
) -> Any:
    """构造带 tool_call 属性的伪 request，模拟 DeepAgents 中间件入参。"""
    args: dict[str, Any] = {}
    if file_path:
        args["file_path"] = file_path
    if content:
        args["content"] = content
    if old_string:
        args["old_string"] = old_string
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id}
    )


def _ok_handler(_req: Any) -> Any:
    """返回成功 ToolMessage 的 handler。"""
    return ToolMessage(content="Updated file /x.md", name="write_file", tool_call_id="t1")


def _error_handler(_req: Any) -> Any:
    """返回 error ToolMessage 的 handler（模拟 deepagents write 吞 OSError）。"""
    return ToolMessage(
        content="Error: Cannot write to /x.md because it already exists.",
        name="write_file",
        tool_call_id="t1",
        status="error",
    )


# ======================================================================
# WriteResultInspectorMiddleware（A2-D3）
# ======================================================================


class WriteResultInspectorTest(unittest.TestCase):
    """验证 D3：write_file/edit_file 返回 error 时转抛 WriteFailedError。"""

    def setUp(self) -> None:
        self.mw = WriteResultInspectorMiddleware()

    def test_error_tool_message_raises_write_failed(self) -> None:
        """write_file 返回 error ToolMessage → 转抛 WriteFailedError（ErrorRecovery 可 catch）。"""
        with self.assertRaises(WriteFailedError):
            self.mw.wrap_tool_call(_request("write_file", file_path="/x.md"), _error_handler)

    def test_success_tool_message_passthrough(self) -> None:
        """write_file 正常返回 → 透传，不误报。"""
        result = self.mw.wrap_tool_call(
            _request("write_file", file_path="/x.md"), _ok_handler
        )
        self.assertIsInstance(result, ToolMessage)
        self.assertNotEqual(getattr(result, "status", None), "error")

    def test_edit_file_also_inspected(self) -> None:
        """edit_file 返回 error → 同样转抛。"""
        with self.assertRaises(WriteFailedError):
            self.mw.wrap_tool_call(_request("edit_file", file_path="/x.md"), _error_handler)

    def test_non_write_tool_passthrough_even_on_error(self) -> None:
        """非写入工具（如 read_file/list_files）返回 error → 透传，不误报。"""
        def read_handler(_req: Any) -> Any:
            return ToolMessage(
                content="File not found",
                name="read_file",
                tool_call_id="t1",
                status="error",
            )
        # 不应该抛
        result = self.mw.wrap_tool_call(
            _request("read_file", file_path="/x.md"), read_handler
        )
        self.assertEqual(getattr(result, "status", None), "error")

    def test_non_toolmessage_result_passthrough(self) -> None:
        """handler 返回非 ToolMessage（如字符串）→ 透传。"""
        def str_handler(_req: Any) -> Any:
            return "some string"
        result = self.mw.wrap_tool_call(
            _request("write_file", file_path="/x.md"), str_handler
        )
        self.assertEqual(result, "some string")

    def test_async_error_raises(self) -> None:
        """异步路径：error ToolMessage → 转抛 WriteFailedError。"""
        async def async_error_handler(_req: Any) -> Any:
            return _error_handler(_req)
        with self.assertRaises(WriteFailedError):
            asyncio.run(
                self.mw.awrap_tool_call(
                    _request("write_file", file_path="/x.md"), async_error_handler
                )
            )


# ======================================================================
# EncodingGuardMiddleware（A2-D1：截断检测 + 去 latin-1 + 不抛异常）
# ======================================================================


class EncodingGuardWriteValidationTest(unittest.TestCase):
    """验证 D1：写入后编码 + 完整性校验。"""

    def setUp(self) -> None:
        self.mw = EncodingGuardMiddleware()

    def _make_write_handler(self, target_path: Path, content_bytes: bytes) -> Any:
        """构造一个 handler：把指定字节写到 target_path（模拟 deepagents write）。"""
        def handler(_req: Any) -> Any:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content_bytes)
            return ToolMessage(
                content=f"Updated file {target_path}", name="write_file", tool_call_id="t1"
            )
        return handler

    def test_normal_write_passes(self) -> None:
        """正常 UTF-8 写入 + 完整 → 通过校验。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "chapter-01.md"
            content = "# 第1章\n\n这是测试正文。"
            handler = self._make_write_handler(target, content.encode("utf-8"))
            result = self.mw.wrap_tool_call(
                _request("write_file", file_path=str(target), content=content), handler
            )
            self.assertNotEqual(getattr(result, "status", None), "error")
            self.assertTrue(target.exists())

    def test_truncated_write_detected_and_file_unlinked(self) -> None:
        """D1 关键：磁盘字节明显少于 args.content → 检测为截断，删除文件，返回 error。

        注意：必须用合法 UTF-8 字节序列模拟截断，否则编码校验会先触发
        （真实写入层截断可能两种都触发，这里专注测完整性校验路径）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "chapter-01.md"
            # 100 个 ASCII 'a'（合法 UTF-8，避免触发编码校验）
            full_content = "a" * 100
            # 故意只写前 10 字节（合法 UTF-8，但内容被截断）
            truncated_bytes = full_content.encode("utf-8")[:10]
            handler = self._make_write_handler(target, truncated_bytes)
            result = self.mw.wrap_tool_call(
                _request("write_file", file_path=str(target), content=full_content), handler
            )
            # 返回 error ToolMessage
            self.assertIsInstance(result, ToolMessage)
            self.assertEqual(result.status, "error")
            assert isinstance(result.content, str)
            self.assertIn("截断", result.content)
            # 文件已被删除（避免下游消费截断文件）
            self.assertFalse(target.exists())

    def test_non_utf8_bytes_detected(self) -> None:
        """D1：写入非法 UTF-8 字节 → 编码校验失败，删除文件，返回 error。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bad.md"
            # 0x8c 是孤立的 continuation byte，非法 UTF-8
            handler = self._make_write_handler(target, b"\x8c\x8d\x8e")
            result = self.mw.wrap_tool_call(
                _request("write_file", file_path=str(target), content="dummy"), handler
            )
            self.assertIsInstance(result, ToolMessage)
            self.assertEqual(result.status, "error")
            assert isinstance(result.content, str)
            self.assertIn("编码", result.content)
            self.assertFalse(target.exists())

    def test_no_truncation_check_when_disabled(self) -> None:
        """check_truncation=False → 跳过完整性校验，只校验编码。

        必须用合法 UTF-8 模拟截断，避免编码校验先触发。
        """
        mw = EncodingGuardMiddleware(check_truncation=False)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.md"
            full_content = "a" * 100
            truncated_bytes = full_content.encode("utf-8")[:10]
            handler = self._make_write_handler(target, truncated_bytes)
            result = mw.wrap_tool_call(
                _request("write_file", file_path=str(target), content=full_content), handler
            )
            # 截断检测关闭 + 编码合法 → 不拦截
            self.assertNotEqual(getattr(result, "status", None), "error")

    def test_empty_file_passes(self) -> None:
        """空文件 → 视为合法（LLM 可能确实写了空内容）。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "empty.md"
            handler = self._make_write_handler(target, b"")
            result = self.mw.wrap_tool_call(
                _request("write_file", file_path=str(target), content=""), handler
            )
            self.assertNotEqual(getattr(result, "status", None), "error")

    def test_does_not_raise_exception(self) -> None:
        """R1 关键：校验失败时返回 error ToolMessage，不抛异常（让 ErrorRecovery 不无脑重试）。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bad.md"
            handler = self._make_write_handler(target, b"\x8c")
            # 不应该抛异常
            try:
                result = self.mw.wrap_tool_call(
                    _request("write_file", file_path=str(target), content="x"), handler
                )
                self.assertIsInstance(result, ToolMessage)
            except Exception as e:
                self.fail(f"EncodingGuard 不应抛异常，但抛了 {type(e).__name__}: {e}")


class EncodingGuardReadFallbackTest(unittest.TestCase):
    """验证 D1：read 降级链只保留 utf-8-sig（移除 latin-1 洗白）。"""

    def test_latin1_corrupted_file_not_whitewashed(self) -> None:
        """关键：损坏 UTF-8 文件不能用 latin-1 洗白成乱码正常返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "corrupt.md"
            # 0x8c 在 latin-1 下能解码成控制字符（"洗白"风险）
            target.write_bytes(b"\x8c\x8d\x8e")

            mw = EncodingGuardMiddleware()
            # read 时：UTF-8 解码失败，utf-8-sig 也失败 → 应返回 error 而不是 latin-1 乱码
            def passthrough_handler(_req: Any) -> Any:
                return "should not reach"

            result = mw.wrap_tool_call(
                _request("read_file", file_path=str(target)), passthrough_handler
            )
            self.assertIsInstance(result, ToolMessage)
            self.assertEqual(result.status, "error")
            assert isinstance(result.content, str)
            self.assertIn("编码错误", result.content)

    def test_utf8_sig_file_uses_fallback(self) -> None:
        """D1 修复点：read 降级链只保留 utf-8-sig。

        UTF-8-SIG（BOM）内容：UTF-8 实际能解码 BOM（被当作 U+FEFF 字符），
        所以走正常路径——这里直接测 _read_with_fallback 方法本身，
        确认它能正确解码 utf-8-sig 文件（移除 latin-1/gbk 后唯一保留的降级）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bom.md"
            # UTF-8 BOM + 中文内容（"标题"）
            target.write_bytes(b"\xef\xbb\xbf# \xe6\xa0\x87\xe9\xa2\x98\n")

            mw = EncodingGuardMiddleware()
            content = mw._read_with_fallback(target)
            self.assertIsNotNone(content)
            assert content is not None
            self.assertIn("标题", content)

    def test_corrupted_bytes_return_none_in_fallback(self) -> None:
        """D1 修复点：损坏字节（非 utf-8-sig 可解）→ _read_with_fallback 返回 None。

        关键：不再有 latin-1 兜底，损坏文件不会被洗白成乱码正常返回。
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "corrupt.md"
            # 0x8c 是孤立 continuation byte，UTF-8 和 utf-8-sig 都解不了
            target.write_bytes(b"\x8c\x8d\x8e")

            mw = EncodingGuardMiddleware()
            content = mw._read_with_fallback(target)
            self.assertIsNone(content, "latin-1 移除后，损坏字节不应被洗白")


# ======================================================================
# ReadCacheMiddleware（A2-D2：写后失效钩子）
# ======================================================================


class ReadCacheWriteInvalidationTest(unittest.TestCase):
    """验证 D2：write_file/edit_file 触发后，对应 file_path 缓存失效。"""

    def setUp(self) -> None:
        self.mw = ReadCacheMiddleware(ttl_seconds=600, track_stats=False)

    def _make_read_handler(self, content: str) -> Any:
        """构造 read handler：返回 content 作为 ToolMessage。"""
        def handler(_req: Any) -> Any:
            return ToolMessage(
                content=content, name="read_file", tool_call_id="t1"
            )
        return handler

    def test_write_file_invalidates_cache(self) -> None:
        """D2 关键：read → write → read，第二次 read 应 miss（重读磁盘）。"""
        path = "/chapter/x.md"

        # 第一次 read：缓存 v1 内容
        read_v1 = self._make_read_handler("version-1")
        self.mw.wrap_tool_call(_request("read_file", file_path=path), read_v1)
        self.assertEqual(self.mw.stats.get("hits", 0), 0) if self.mw.track_stats else None

        # write_file 触发 → 应清除缓存
        def write_handler(_req: Any) -> Any:
            return ToolMessage(content="Updated", name="write_file", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("write_file", file_path=path), write_handler)

        # 第二次 read：不应命中缓存（应该重读）
        # 用一个会标记是否被调用的 handler 验证
        handler_called = {"n": 0}
        def read_v2(_req: Any) -> Any:
            handler_called["n"] += 1
            return ToolMessage(content="version-2", name="read_file", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("read_file", file_path=path), read_v2)

        self.assertEqual(handler_called["n"], 1, "第二次 read 应该实际调用 handler（缓存已失效）")

    def test_edit_file_invalidates_cache(self) -> None:
        """edit_file 也应触发缓存失效。"""
        path = "/chapter/y.md"
        self.mw.wrap_tool_call(
            _request("read_file", file_path=path), self._make_read_handler("v1")
        )

        def edit_handler(_req: Any) -> Any:
            return ToolMessage(content="Edited", name="edit_file", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("edit_file", file_path=path), edit_handler)

        handler_called = {"n": 0}
        def read_v2(_req: Any) -> Any:
            handler_called["n"] += 1
            return ToolMessage(content="v2", name="read_file", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("read_file", file_path=path), read_v2)
        self.assertEqual(handler_called["n"], 1, "edit 后 read 应实际调用 handler")

    def test_read_cache_hit_avoids_handler(self) -> None:
        """无写操作时，重复 read 应命中缓存（不调用 handler）。"""
        path = "/chapter/z.md"
        self.mw.wrap_tool_call(
            _request("read_file", file_path=path), self._make_read_handler("v1")
        )

        handler_called = {"n": 0}
        def read_again(_req: Any) -> Any:
            handler_called["n"] += 1
            return ToolMessage(content="v2", name="read_file", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("read_file", file_path=path), read_again)
        self.assertEqual(handler_called["n"], 0, "命中缓存不应调 handler")

    def test_other_tool_does_not_invalidate(self) -> None:
        """非读写工具（如 list_files）不应清缓存。"""
        path = "/chapter/w.md"
        self.mw.wrap_tool_call(
            _request("read_file", file_path=path), self._make_read_handler("v1")
        )

        def list_handler(_req: Any) -> Any:
            return ToolMessage(content="files...", name="list_files", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("list_files"), list_handler)

        handler_called = {"n": 0}
        def read_again(_req: Any) -> Any:
            handler_called["n"] += 1
            return ToolMessage(content="v2", name="read_file", tool_call_id="t1")
        self.mw.wrap_tool_call(_request("read_file", file_path=path), read_again)
        self.assertEqual(handler_called["n"], 0, "list_files 不应清缓存")


# ======================================================================
# FileStateTrackerMiddleware（A2-D4：修剪死代码后）
# ======================================================================


class FileStateTrackerPrunedTest(unittest.TestCase):
    """验证 D4：edit_file 前 old_string 预检 + 死代码已删。"""

    def setUp(self) -> None:
        self.mw = FileStateTrackerMiddleware()

    def test_no_file_states_attribute(self) -> None:
        """D4 关键：死代码 _file_states 字段已被删除。"""
        self.assertFalse(
            hasattr(self.mw, "_file_states"),
            "_file_states 死代码字段应已被删除",
        )
        self.assertFalse(
            hasattr(self.mw, "_update_file_states"),
            "_update_file_states 死代码方法应已被删除",
        )
        self.assertFalse(hasattr(self.mw, "track_extensions"))

    def test_edit_with_existing_old_string_passes(self) -> None:
        """old_string 真实存在 → 放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.md"
            target.write_text("# 标题\n\n正文内容。\n", encoding="utf-8")

            handler_called = {"n": 0}
            def handler(_req: Any) -> Any:
                handler_called["n"] += 1
                return ToolMessage(content="Edited", name="edit_file", tool_call_id="t1")

            result = self.mw.wrap_tool_call(
                _request("edit_file", file_path=str(target), old_string="正文内容。"), handler
            )
            self.assertEqual(handler_called["n"], 1, "old_string 存在应放行 handler")

    def test_edit_with_missing_old_string_blocked(self) -> None:
        """old_string 不在文件中 → 拦截，返回 error ToolMessage。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.md"
            target.write_text("# 标题\n\n其他内容。\n", encoding="utf-8")

            handler_called = {"n": 0}
            def handler(_req: Any) -> Any:
                handler_called["n"] += 1
                return "should-not-reach"

            result = self.mw.wrap_tool_call(
                _request("edit_file", file_path=str(target), old_string="不存在的字符串"), handler
            )
            self.assertEqual(handler_called["n"], 0, "old_string 不存在应拦截 handler")
            self.assertIsInstance(result, ToolMessage)
            self.assertEqual(result.status, "error")
            assert isinstance(result.content, str)
            self.assertIn("read_file", result.content)

    def test_edit_nonexistent_file_passes_through(self) -> None:
        """edit_file 目标文件不存在 → 放行让 edit_file 自己报错。"""
        handler_called = {"n": 0}
        def handler(_req: Any) -> Any:
            handler_called["n"] += 1
            return ToolMessage(content="ok", name="edit_file", tool_call_id="t1")

        self.mw.wrap_tool_call(
            _request("edit_file", file_path="/nonexistent.md", old_string="x"), handler
        )
        self.assertEqual(handler_called["n"], 1)

    def test_non_edit_tool_passthrough(self) -> None:
        """非 edit_file 工具 → 完全透传。"""
        handler_called = {"n": 0}
        def handler(_req: Any) -> Any:
            handler_called["n"] += 1
            return "ok"

        self.mw.wrap_tool_call(_request("read_file", file_path="/x.md"), handler)
        self.assertEqual(handler_called["n"], 1)


if __name__ == "__main__":
    unittest.main()
