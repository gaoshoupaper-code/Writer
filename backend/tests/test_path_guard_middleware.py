from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.writer.middleware.path_guard_middleware import normalize_workspace_write_path


class NormalizeWorkspaceWritePathTest(unittest.TestCase):
    def test_accepts_character_markdown_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            self.assertEqual(
                normalize_workspace_write_path("character/林映真.md", workspace),
                "/character/林映真.md",
            )
            self.assertEqual(
                normalize_workspace_write_path("/character/林映真.md", workspace),
                "/character/林映真.md",
            )
            self.assertEqual(
                normalize_workspace_write_path(r"character\林映真.md", workspace),
                "/character/林映真.md",
            )

    def test_accepts_outline_evaluation_novel_and_review_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            self.assertEqual(normalize_workspace_write_path("outline.md", workspace), "/outline.md")
            self.assertEqual(normalize_workspace_write_path("/outline.md", workspace), "/outline.md")
            self.assertEqual(normalize_workspace_write_path("evaluation.md", workspace), "/evaluation.md")
            self.assertEqual(normalize_workspace_write_path("/evaluation.md", workspace), "/evaluation.md")
            self.assertEqual(normalize_workspace_write_path("novel.md", workspace), "/novel.md")
            self.assertEqual(normalize_workspace_write_path("/novel.md", workspace), "/novel.md")
            self.assertEqual(normalize_workspace_write_path("review/chapter-01.md", workspace), "/review/chapter-01.md")
            self.assertEqual(normalize_workspace_write_path("/review/chapter-01.md", workspace), "/review/chapter-01.md")

    def test_accepts_workspace_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            target = workspace / "character" / "林映真.md"

            self.assertEqual(
                normalize_workspace_write_path(str(target), workspace),
                "/character/林映真.md",
            )
            self.assertEqual(
                normalize_workspace_write_path("\\\\?\\" + str(target), workspace),
                "/character/林映真.md",
            )

    def test_rejects_unsafe_or_out_of_scope_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            rejected_paths = [
                "../secret.md",
                "/../secret.md",
                "~/secret.md",
                "//server/share/a.md",
                "/character/../outline.md",
                "/character/a.txt",
                "/character/a/b.md",
                "/review.md",
                "/review/a.txt",
                "/review/a/b.md",
                "/anything.md",
            ]

            for path in rejected_paths:
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        normalize_workspace_write_path(path, workspace)

    def test_rejects_workspace_external_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as external_dir:
            workspace = Path(workspace_dir).resolve()
            external = Path(external_dir).resolve() / "character" / "林映真.md"

            with self.assertRaises(ValueError):
                normalize_workspace_write_path(str(external), workspace)

    def test_rejects_empty_or_non_string_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            for path in ("", "   ", None, 123):
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        normalize_workspace_write_path(path, workspace)


if __name__ == "__main__":
    unittest.main()
