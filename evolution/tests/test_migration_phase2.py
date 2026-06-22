"""Phase 6 迁移脚本测试（T2.1/T2.3）。

覆盖：
- 迁移导入 30 个 surface（各类计数正确）
- 生成 production manifest（surfaces 完整 + schema_lock 捕获 C 类）
- 等价性：导入的 surface 内容与 v1 源一致（spot check 各类）
- 幂等：重复迁移不产生重复数据
- dry-run / import-only 模式
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        os.environ["EVOLUTION_DB"] = str(Path(self._tmpdir) / "test.db")
        os.environ["EXECUTOR_WORKSPACE"] = self._tmpdir
        import importlib
        import app.core.settings as settings_mod
        importlib.reload(settings_mod)
        import app.core.db as db
        db._conn = None
        db.init_db()
        self.db = db
        # 重新 import 迁移模块（拿到当前 settings 的 db 连接）
        import app.migrate_to_surface as migrate
        importlib.reload(migrate)
        self.migrate = migrate

    def tearDown(self) -> None:
        try:
            self.db.get_conn().close()
        except Exception:
            pass

    # ── 完整迁移 ─────────────────────────────────────────────

    def test_full_migration_counts(self) -> None:
        """完整迁移导入各类 surface 数量正确。"""
        from app.improvement import surface_repo
        result = self.migrate.run_migration()
        self.assertEqual(result["surfaces_imported"], 30)
        self.assertEqual(result["by_category"]["prompt"], 8)
        self.assertEqual(result["by_category"]["skill"], 6)
        self.assertEqual(result["by_category"]["description"], 4)
        self.assertEqual(result["by_category"]["middleware_params"], 5)
        self.assertEqual(result["by_category"]["deep_meta"], 3)  # deep 装配元数据独立计数
        self.assertEqual(result["by_category"]["permissions"], 3)
        self.assertEqual(result["by_category"]["stateful_middleware"], 1)
        self.assertIsNotNone(result["manifest"])

    def test_production_manifest_generated(self) -> None:
        """迁移后生成 production manifest，30 surfaces + GoalMiddleware 进 schema_lock。"""
        from app.improvement import manifest_repo
        self.migrate.run_migration()
        prod = manifest_repo.get_production_manifest()
        self.assertIsNotNone(prod)
        self.assertEqual(prod["status"], "production")
        entries = manifest_repo.get_entries(prod)
        self.assertEqual(len(entries["surfaces"]), 30)
        c_names = {(s["surface_name"], s["scope"]) for s in entries["schema_lock"]["c_surfaces"]}
        self.assertIn(("GoalMiddleware", "meta"), c_names)

    # ── 等价性（与 v1 源对比）──────────────────────────────

    def test_equivalence_prompt_content(self) -> None:
        """导入的 prompt 内容与 .md 源文件一致。"""
        from app.improvement import surface_repo
        self.migrate.run_migration()
        wsys = surface_repo.get_approved_version("prompt", "writing_system", "writing")
        self.assertIsNotNone(wsys)
        # 直接读源文件对比
        src = self.migrate._PROMPTS_DIR / "writing_system.md"
        self.assertEqual(wsys["content"], src.read_text(encoding="utf-8"))

    def test_equivalence_c_code_complete(self) -> None:
        """C 类 GoalMiddleware 代码完整导入（含 class 定义 + state_schema）。"""
        from app.improvement import surface_repo
        self.migrate.run_migration()
        gm = surface_repo.get_approved_version("stateful_middleware", "GoalMiddleware", "meta")
        self.assertIsNotNone(gm)
        self.assertIn("class GoalMiddleware", gm["content"])
        self.assertIn("state_schema = GoalState", gm["content"])
        self.assertEqual(gm["content_kind"], "python")
        # config 记录了 state_schema_ref + channels（供执行端/回放追溯）
        import json
        config = json.loads(gm["config"])
        self.assertEqual(config["state_schema_ref"], "app.domains.writing.tools.GoalState")
        self.assertIn("goal", config["state_channels"])

    def test_equivalence_b_params(self) -> None:
        """B 类 ContextAssembler 各 scope 参数正确（验证同名跨 scope 独立）。"""
        from app.improvement import surface_repo
        import json
        self.migrate.run_migration()
        # storybuilding: 只 demand.md
        sb = surface_repo.get_approved_version("middleware_params", "ContextAssembler", "storybuilding")
        sb_args = json.loads(sb["content"])["args"]
        self.assertEqual(sb_args["file_paths"], ["demand.md"])
        self.assertEqual(sb_args["context_label"], "创作需求")
        # writing: 含 detail/*.md + 写作前置 label
        wr = surface_repo.get_approved_version("middleware_params", "ContextAssembler", "writing")
        wr_args = json.loads(wr["content"])["args"]
        self.assertIn("detail/*.md", wr_args["file_paths"])
        self.assertEqual(wr_args["context_label"], "写作前置上下文")

    def test_equivalence_permissions_order(self) -> None:
        """permissions 顺序敏感（deny /** 必须在 allow 之后）。"""
        from app.improvement import surface_repo
        import json
        self.migrate.run_migration()
        perm = surface_repo.get_approved_version(
            "permissions", "permissions/storybuilding", "storybuilding")
        rules = json.loads(perm["content"])
        # 最后一条应是 deny /**
        self.assertEqual(rules[-1]["mode"], "deny")
        self.assertIn("/**", rules[-1]["paths"])
        # 之前的 allow 规则在前
        self.assertEqual(rules[0]["operations"], ["read"])

    def test_equivalence_deep_meta(self) -> None:
        """deep subagent 装配元数据（evaluator_kind/max_revisions）正确。"""
        from app.improvement import surface_repo
        import json
        self.migrate.run_migration()
        meta = surface_repo.get_approved_version(
            "middleware_params", "deep_meta/storybuilding", "storybuilding")
        data = json.loads(meta["content"])
        self.assertEqual(data["evaluator_kind"], "storybuilding")
        self.assertEqual(data["max_revisions"], 1)
        self.assertIn("storyline.md", data["artifact_paths"])

    # ── 幂等 ─────────────────────────────────────────────────

    def test_idempotent(self) -> None:
        """重复迁移不产生重复数据（重建基准语义）。"""
        from app.improvement import surface_repo, manifest_repo
        self.migrate.run_migration()
        count1 = len(surface_repo.list_all_approved_grouped())
        # 再跑一次
        self.migrate.run_migration()
        count2 = len(surface_repo.list_all_approved_grouped())
        self.assertEqual(count1, 30)
        self.assertEqual(count2, 30)  # 不翻倍

    # ── 模式 ─────────────────────────────────────────────────

    def test_dry_run_no_write(self) -> None:
        """dry-run 不写库。"""
        from app.improvement import surface_repo
        result = self.migrate.run_migration(dry_run=True)
        self.assertEqual(result["surfaces_imported"], 30)
        self.assertIsNone(result["manifest"])
        # 库应为空
        self.assertEqual(len(surface_repo.list_all_approved_grouped()), 0)

    def test_import_only_no_manifest(self) -> None:
        """import-only 只导 surface 不发布 manifest。"""
        from app.improvement import surface_repo, manifest_repo
        result = self.migrate.run_migration(import_only=True)
        self.assertEqual(result["surfaces_imported"], 30)
        self.assertIsNone(result["manifest"])
        # surface 导入了
        self.assertEqual(len(surface_repo.list_all_approved_grouped()), 30)
        # 但无 manifest
        self.assertIsNone(manifest_repo.get_production_manifest())

    # ── scope 分布 ───────────────────────────────────────────

    def test_scope_coverage(self) -> None:
        """迁移覆盖所有 scope（meta/writing/storybuilding/detail-outline/interview）。"""
        from app.improvement import surface_repo
        self.migrate.run_migration()
        for scope in ("meta", "writing", "storybuilding", "detail-outline", "interview"):
            rows = surface_repo.list_by_scope(scope, status="approved")
            self.assertGreater(len(rows), 0, f"scope {scope} 无 surface")


if __name__ == "__main__":
    unittest.main()
