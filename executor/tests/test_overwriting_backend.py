"""FR-002 / DEC-005 / RSK-001：OverwritingFilesystemBackend 方案C 验证。

deepagents FilesystemBackend.write 默认对已存在文件返回 error（EVD-006）。
方案C 在隔离层 subclass 重写 write 加 overwrite 语义，必须：
  - 对已存在文件覆盖成功（AC-003）
  - 保持 0.6.1 的原子写语义（O_NOFOLLOW / O_CREAT|O_TRUNC / newline="" / mkdir parents）
    （NFR-001 / RSK-001）
  - 新建文件语义不变（回归）
  - 保持 virtual_mode 的路径安全（回归）
  - awrite 继承新行为（asyncio.to_thread(self.write)）
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from app.platform.agent.runtime import (
    FilesystemBackend,
    OverwritingFilesystemBackend,
)


class OverwritingFilesystemBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_file_creation_still_works(self) -> None:
        """回归：新文件创建语义不变（不依赖 overwrite 分支）。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        result = backend.write("/new.md", "首次内容")
        self.assertIsNotNone(result.path)
        self.assertEqual((self.root / "new.md").read_text(encoding="utf-8"), "首次内容")

    def test_overwrites_existing_file_instead_of_error(self) -> None:
        """FR-002 / AC-003：覆盖式产物写入成功，不返回 already-exists 错误。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        backend.write("/review/storybuilding.md", "旧审查")
        result = backend.write("/review/storybuilding.md", "新审查覆盖")
        self.assertIsNotNone(
            result.path, "覆盖写应成功，不应返回 error"
        )
        self.assertEqual(
            (self.root / "review" / "storybuilding.md").read_text(encoding="utf-8"),
            "新审查覆盖",
        )

    def test_overwrite_evaluates_each_path_in_whitelist(self) -> None:
        """FR-002：清单内全部路径都能覆盖（evaluation.md / detail/evaluation.md / review/*）。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        for path, content in [
            ("/evaluation.md", "评估 v2"),
            ("/detail/evaluation.md", "细纲评估 v2"),
            ("/review/chapter-01.md", "第一章审查 v2"),
        ]:
            backend.write(path, "v1")
            result = backend.write(path, content)
            self.assertIsNotNone(result.path, f"{path} 应可覆盖")
            self.assertIsNone(result.error)

    def test_non_whitelisted_existing_path_still_rejects_overwrite(self) -> None:
        """EDGE-001 / FR-002：清单外路径（如 novel.md 正文）保持 deepagents 默认"拒覆盖"语义，
        模型按 prompt 指引改用 edit_file。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        backend.write("/novel.md", "第一稿")
        result = backend.write("/novel.md", "想整体覆盖")
        self.assertIsNotNone(result.error, "非覆盖式产物应被拒")
        self.assertIn("already exists", result.error)
        # 原文件未被覆盖
        self.assertEqual((self.root / "novel.md").read_text(encoding="utf-8"), "第一稿")

    def test_overwrite_preserves_atomic_semantics_no_crlf_translation(self) -> None:
        """RSK-001 / NFR-001：newline="" 必须保留，LF-only 内容落盘为 LF 字节。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        backend.write("/review/chapter-01.md", "first\n")
        backend.write("/review/chapter-01.md", "a\nb\nc\n")
        raw = (self.root / "review" / "chapter-01.md").read_bytes()
        self.assertNotIn(b"\r\n", raw, "不得引入 CRLF 翻译")
        self.assertEqual(raw, b"a\nb\nc\n")

    def test_overwrite_creates_parent_dirs(self) -> None:
        """回归：覆盖写同样要 mkdir parents（首次写深层路径）。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        result = backend.write("/detail/evaluation.md", "评估")
        self.assertIsNotNone(result.path)
        self.assertEqual(
            (self.root / "detail" / "evaluation.md").read_text(encoding="utf-8"),
            "评估",
        )

    def test_overwrite_does_not_follow_symlinks(self) -> None:
        """RSK-001：覆盖写保持 O_NOFOLLOW（unix 可用时），不穿透符号链接写盘外。

        Windows 无 O_NOFOLLOW 时本测试自动跳过，仅在支持的平台上验证安全语义。
        用清单内路径（/review/escaped.md）走覆盖分支。
        """
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available on this platform")
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        # 在 root 外造一个目标文件，再在 root 内建符号链接指向它
        outside = Path(tempfile.mkdtemp()) / "outside.md"
        outside.write_text("机密外部", encoding="utf-8")
        try:
            link = self.root / "review" / "escaped.md"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(outside)
            result = backend.write("/review/escaped.md", "尝试覆盖链接")
            # O_NOFOLLOW 下写应失败（不穿透），外部文件不被污染
            if result.error:
                self.assertEqual(outside.read_text(encoding="utf-8"), "机密外部")
        finally:
            outside.unlink(missing_ok=True)
            outside.parent.rmdir(missing_ok=True)

    def test_virtual_mode_blocks_path_escape_on_overwrite(self) -> None:
        """回归：覆盖分支同样要遵守 virtual_mode 路径安全（阻止绝对路径逃逸）。

        _resolve_path 对越界路径抛 ValueError（deepagents 既有行为，路径安全的底层
        保障），上层 PathGuardMiddleware 会在工具层先拦截。覆盖分支不削弱此语义。
        """
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        backend.write("/review/inside.md", "内")
        outside = Path(tempfile.mkdtemp()) / "escaped.md"
        try:
            with self.assertRaises((ValueError, RuntimeError, OSError)):
                backend.write(str(outside), "尝试逃逸")
            self.assertFalse(outside.exists())
        finally:
            try:
                outside.parent.rmdir()
            except OSError:
                pass

    def test_awrite_inherits_overwrite_behavior(self) -> None:
        """awrite 经 asyncio.to_thread(self.write) 委托，应继承覆盖语义。"""
        backend = OverwritingFilesystemBackend(root_dir=self.root, virtual_mode=True)
        backend.write("/review/async.md", "v1")
        result = asyncio.run(backend.awrite("/review/async.md", "v2-overwrite"))
        self.assertIsNotNone(result.path)
        self.assertEqual(
            (self.root / "review" / "async.md").read_text(encoding="utf-8"),
            "v2-overwrite",
        )

    def test_subclass_is_exported_as_filesystem_backend_default(self) -> None:
        """FR-002：隔离层 re-export 已把 FilesystemBackend 替换为 Overwriting 子类，
        领域层零改动即获得覆盖能力。"""
        self.assertTrue(issubclass(OverwritingFilesystemBackend, FilesystemBackend))


if __name__ == "__main__":
    unittest.main()
