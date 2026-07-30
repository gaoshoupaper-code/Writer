from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class HarnessProductionCheckoutTest(unittest.TestCase):
    def test_existing_checkout_resets_to_registry_production_commit(self) -> None:
        from app.platform.agent import git_sync

        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "production"
            (checkout / ".git").mkdir(parents=True)
            calls: list[tuple[list[str], Path | None]] = []

            def fake_git(args: list[str], cwd: Path | None = None) -> str:
                calls.append((args, cwd))
                return ""

            with patch.object(git_sync, "production_checkout", return_value=checkout), patch.object(
                git_sync, "_git", side_effect=fake_git
            ), patch.object(
                git_sync, "remote_production_commit", return_value="candidate-commit"
            ):
                result = git_sync.pull_production()

        self.assertEqual(result, checkout)
        self.assertIn((["reset", "--hard", "candidate-commit"], checkout), calls)
        self.assertNotIn((["reset", "--hard", "origin/main"], checkout), calls)


if __name__ == "__main__":
    unittest.main()
