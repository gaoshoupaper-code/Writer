from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.versioning import registry_repo
from app.versioning.middleware_projection import build_middleware_projection


class MiddlewareProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package_root = Path(__file__).resolve().parents[1] / "harnesses" / "repo"
        paths = [
            "__init__.py",
            "subagents/interview.py",
            "subagents/storybuilding.py",
            "subagents/detail_outline.py",
            "subagents/writing.py",
            "subagents/factory.py",
            "subagents/reviewers/storybuilding.py",
            "subagents/reviewers/detail_outline.py",
            "subagents/reviewers/writing.py",
        ]
        paths.extend(
            path.relative_to(cls.package_root).as_posix()
            for path in (cls.package_root / "middleware").glob("*.py")
        )
        cls.stacks = build_middleware_projection(
            {
                path: (cls.package_root / path).read_text(encoding="utf-8")
                for path in paths
            }
        )

    def test_projects_all_direct_and_review_agents(self) -> None:
        self.assertEqual(
            set(self.stacks),
            {
                "meta",
                "general_purpose",
                "interview",
                "storybuilding",
                "storybuilding_review",
                "detail_outline",
                "detail_outline_review",
                "writing",
                "writing_review",
            },
        )

    def test_projects_real_class_names_and_hooks(self) -> None:
        story = {item["class_name"]: item for item in self.stacks["storybuilding"]}
        self.assertEqual(
            story["StorybuildingIterationLimitMiddleware"]["hooks"],
            ["before_agent", "before_model"],
        )
        self.assertEqual(
            story["StorybuildingIterationLimitMiddleware"]["hook"],
            "before_agent",
        )
        self.assertEqual(
            story["StorylineSingleLineLimitMiddleware"]["hooks"],
            ["before_agent", "wrap_tool_call"],
        )
        self.assertNotIn("ContextAssemblerMiddleware", story)
        self.assertFalse(story["ArtifactValidationMiddleware"]["optional"])
        self.assertTrue(
            all(item["hooks"] for stack in self.stacks.values() for item in stack)
        )

    def test_projects_agent_specific_conditions(self) -> None:
        interview = {item["class_name"] for item in self.stacks["interview"]}
        self.assertNotIn("ErrorRecoveryMiddleware", interview)
        self.assertNotIn("CreditsMiddleware", interview)
        self.assertIn("FilesystemPathGuardMiddleware", interview)

        detail = {item["class_name"] for item in self.stacks["detail_outline"]}
        writing = {item["class_name"]: item for item in self.stacks["writing"]}
        self.assertNotIn("ArtifactValidationMiddleware", detail)
        self.assertNotIn("ArtifactValidationMiddleware", writing)
        self.assertTrue(writing["MemoryRecallMiddleware"]["optional"])

        for reviewer in ("detail_outline_review", "writing_review"):
            context = next(
                item
                for item in self.stacks[reviewer]
                if item["class_name"] == "ContextAssemblerMiddleware"
            )
            self.assertFalse(context["optional"])


class RegistryCleanupTest(unittest.TestCase):
    def test_prunes_unbound_history_and_binds_production(self) -> None:
        registry = {
            "schema_version": 1,
            "production": 3,
            "versions": [
                {"version": 1, "parent_version": None, "executable": False},
                {"version": 2, "parent_version": 1, "executable": False},
                {"version": 3, "parent_version": 2, "executable": False},
            ],
            "rollback_log": [{"from": 3, "to": 1}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            with patch("app.versioning.registry_repo._registry_path", return_value=path):
                result = registry_repo.prune_unexecutable_history_and_bind_production("abc1234")
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result["removed_versions"], [1, 2])
        self.assertEqual(saved["rollback_log"], [])
        self.assertEqual(
            saved["versions"],
            [
                {
                    "version": 3,
                    "parent_version": None,
                    "executable": True,
                    "commit_hash": "abc1234",
                }
            ],
        )

    def test_keeps_already_bound_history(self) -> None:
        registry = {
            "schema_version": 1,
            "production": 2,
            "versions": [
                {"version": 1, "parent_version": None, "commit_hash": "old"},
                {"version": 2, "parent_version": 1, "commit_hash": "current"},
            ],
            "rollback_log": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            with patch("app.versioning.registry_repo._registry_path", return_value=path):
                result = registry_repo.prune_unexecutable_history_and_bind_production("ignored")

        self.assertFalse(result["changed"])
        self.assertEqual(result["removed_versions"], [])


if __name__ == "__main__":
    unittest.main()
