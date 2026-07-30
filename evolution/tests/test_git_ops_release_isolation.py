from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch


class RegistryMetadataCommitTest(unittest.TestCase):
    def test_startup_binding_keeps_unstaged_source_out_of_commit(self) -> None:
        from app.core import git_ops

        work_dir = Path("harness")
        calls: list[list[str]] = []

        def git_with_author(args: list[str], _cwd: Path) -> str:
            calls.append(args)
            return ""

        with patch.object(git_ops, "work_dir", return_value=work_dir), patch.object(
            git_ops, "_changed_paths", return_value=[]
        ), patch.object(
            git_ops, "_all_changed_paths",
            return_value={"registry.json", "middleware/artifact_snapshot.py"},
        ), patch.object(
            git_ops, "_git_with_author", side_effect=git_with_author
        ), patch.object(
            git_ops, "_push_to_bare"
        ), patch.object(
            git_ops, "current_commit", return_value="registry-commit"
        ):
            result = git_ops.commit_registry_metadata_and_push("bind production")

        self.assertEqual(result, "registry-commit")
        self.assertEqual(calls[0], ["add", "--", "registry.json"])
        self.assertNotIn("middleware/artifact_snapshot.py", str(calls))

    def test_startup_binding_rejects_pre_staged_source(self) -> None:
        from app.core import git_ops

        with patch.object(git_ops, "work_dir", return_value=Path("harness")), patch.object(
            git_ops, "_changed_paths",
            return_value=["middleware/artifact_snapshot.py"],
        ):
            with self.assertRaisesRegex(RuntimeError, "夹带已暂存源码"):
                git_ops.commit_registry_metadata_and_push("bind production")


if __name__ == "__main__":
    unittest.main()
