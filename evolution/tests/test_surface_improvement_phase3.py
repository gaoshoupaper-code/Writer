"""Phase 6 进化端改造测试（T3.1/T3.2/T3.4/T3.6）。

覆盖：
- proposer surface：resolve_surface_type 映射、_extract_surface_content、prompt 构造
- static_check C 类契约：state_schema 检查、危险模式、真实 GoalMiddleware 通过
- pipeline surface：process_signature_surface 流程、_infer_scope
- A/B scope 过滤：_get_trace_avg_score_filtered 按 target 过滤
- manifest_publisher：approve_and_publish + notify（降级）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SurfaceImprovementTest(unittest.TestCase):
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
        # 导入一些 base surface 供 pipeline 测试用
        from app.improvement import surface_repo
        surface_repo.create_version(
            "prompt", "writing_system", "writing", "原始 prompt",
            status="approved", source="migrated",
        )

    def tearDown(self) -> None:
        try:
            self.db.get_conn().close()
        except Exception:
            pass

    # ── proposer: resolve_surface_type ───────────────────────

    def test_resolve_surface_type_from_explicit(self) -> None:
        """签名自带 surface_type 时优先用它。"""
        from app.improvement import proposer
        sig = {"surface_type": "permissions", "target_component": "prompt"}
        self.assertEqual(proposer.resolve_surface_type(sig), "permissions")

    def test_resolve_surface_type_from_component(self) -> None:
        """无 surface_type 时从 target_component 映射。"""
        from app.improvement import proposer
        self.assertEqual(proposer.resolve_surface_type({"target_component": "prompt"}), "prompt")
        self.assertEqual(proposer.resolve_surface_type({"target_component": "skill"}), "skill")

    def test_resolve_surface_type_goal_middleware_is_c_code(self) -> None:
        """GoalMiddleware（带 state_schema）特判成 C 类。"""
        from app.improvement import proposer
        sig = {"target_component": "middleware", "target_ref": "GoalMiddleware"}
        self.assertEqual(proposer.resolve_surface_type(sig), "stateful_middleware")

    def test_resolve_surface_type_plain_middleware_is_b_param(self) -> None:
        """普通 middleware（无 state_schema）是 B 类参数。"""
        from app.improvement import proposer
        sig = {"target_component": "middleware", "target_ref": "StorylineSingleLineLimit"}
        self.assertEqual(proposer.resolve_surface_type(sig), "middleware_params")

    def test_resolve_surface_type_invalid_falls_back(self) -> None:
        """非法 surface_type 回退到 target_component 映射。"""
        from app.improvement import proposer
        sig = {"surface_type": "bad_type", "target_component": "prompt"}
        self.assertEqual(proposer.resolve_surface_type(sig), "prompt")

    # ── proposer: _extract_surface_content ───────────────────

    def test_extract_python_from_code_fence(self) -> None:
        from app.improvement import proposer
        result = proposer._extract_surface_content("text\n```python\nclass X: pass\n```\nmore", "python")
        self.assertEqual(result, "class X: pass")

    def test_extract_json_from_code_fence(self) -> None:
        from app.improvement import proposer
        result = proposer._extract_surface_content("```json\n{\"a\": 1}\n```", "json")
        self.assertEqual(result, '{"a": 1}')

    def test_extract_text_strips_fence(self) -> None:
        """text 类去掉多余的代码块包裹。"""
        from app.improvement import proposer
        self.assertEqual(proposer._extract_surface_content("hello", "text"), "hello")
        self.assertEqual(proposer._extract_surface_content("```\nplain\n```", "text"), "plain")

    def test_extract_python_fallback_raw(self) -> None:
        """python 无代码块但像代码时，整个输出当代码。"""
        from app.improvement import proposer
        result = proposer._extract_surface_content("class Foo: pass", "python")
        self.assertEqual(result, "class Foo: pass")

    # ── proposer: prompt 构造（按层分发）────────────────────

    def test_prompt_c_class_has_state_schema_requirement(self) -> None:
        """C 类 prompt 含 state_schema 契约要求。"""
        from app.improvement import proposer
        sig = {"signature_text": "拦截过严", "target_ref": "GoalMiddleware",
               "surface_scope": "meta", "layer": "l", "target": "t", "metric": "m"}
        prompt = proposer._build_surface_proposer_prompt(sig, "stateful_middleware", "code", None)
        self.assertIn("state_schema", prompt)
        self.assertIn("os.system", prompt)  # 危险模式禁令

    def test_prompt_b_class_keeps_placeholders(self) -> None:
        """B 类 prompt 要求保持 ${ctx.xxx} 占位符。"""
        from app.improvement import proposer
        sig = {"signature_text": "参数问题", "target_ref": "ContextAssembler",
               "surface_scope": "writing"}
        prompt = proposer._build_surface_proposer_prompt(sig, "middleware_params", "{}", None)
        self.assertIn("${ctx.xxx}", prompt)  # 占位符保持要求
        self.assertIn("JSON", prompt)

    def test_prompt_a_class_minimal_change(self) -> None:
        """A 类 prompt 强调最小改动。"""
        from app.improvement import proposer
        sig = {"signature_text": "prompt 问题", "target_ref": "writing_system",
               "surface_scope": "writing"}
        prompt = proposer._build_surface_proposer_prompt(sig, "prompt", "原文", None)
        self.assertIn("最小", prompt)

    # ── static_check: C 类契约 ───────────────────────────────

    def test_validate_python_real_goal_middleware_passes(self) -> None:
        """真实 GoalMiddleware 代码通过 C 类契约检查。"""
        from app.improvement.static_check import validate_python_surface
        # 构造一个满足契约的最小片段（带 state_schema 的 middleware）
        code = (
            "from langchain.agents.middleware.types import AgentMiddleware\n"
            "class GoalMiddleware(AgentMiddleware):\n"
            "    state_schema = dict\n"
            "    def after_model(self, state, runtime):\n"
            "        return None\n"
        )
        ok, errs = validate_python_surface(code, {})
        self.assertTrue(ok, f"应通过但报错: {errs}")

    def test_validate_python_rejects_no_state_schema(self) -> None:
        """缺 state_schema 的 middleware 被拒。"""
        from app.improvement.static_check import validate_python_surface
        code = (
            "from langchain.agents.middleware.types import AgentMiddleware\n"
            "class BadMiddleware(AgentMiddleware):\n"
            "    def after_model(self, state, runtime):\n"
            "        return None\n"
        )
        ok, errs = validate_python_surface(code, {})
        self.assertFalse(ok)
        self.assertTrue(any("state_schema" in e for e in errs))

    def test_validate_python_rejects_no_middleware_class(self) -> None:
        """无 Middleware 类被拒。"""
        from app.improvement.static_check import validate_python_surface
        ok, errs = validate_python_surface("def foo(): return 1\n", {})
        self.assertFalse(ok)

    def test_validate_python_rejects_dangerous(self) -> None:
        """危险模式（os.system 调用）被拒。"""
        from app.improvement.static_check import validate_python_surface
        code = (
            "import os\n"
            "class DMiddleware:\n"
            "    state_schema = dict\n"
            "    def run(self):\n"
            "        os.system('rm -rf /')\n"
        )
        ok, errs = validate_python_surface(code, {})
        self.assertFalse(ok)
        self.assertTrue(any("C4" in e for e in errs))

    # ── pipeline: _infer_scope ───────────────────────────────

    def test_infer_scope_from_target(self) -> None:
        """签名无 surface_scope 时从 target 推断。"""
        from app.improvement import pipeline
        self.assertEqual(pipeline._infer_scope({"target": "writing"}), "writing")
        self.assertEqual(pipeline._infer_scope({"target": "storybuilding"}), "storybuilding")

    def test_infer_scope_global_for_unknown(self) -> None:
        """未知 target 归 global。"""
        from app.improvement import pipeline
        self.assertEqual(pipeline._infer_scope({"target": "novel"}), "global")

    # ── pipeline: A/B scope 过滤 ─────────────────────────────

    def test_scope_to_eval_targets_mapping(self) -> None:
        """scope → eval_targets 映射覆盖所有 scope。"""
        from app.improvement import pipeline
        for scope in ("writing", "storybuilding", "detail-outline", "interview"):
            self.assertIn(scope, pipeline._SCOPE_TO_EVAL_TARGETS)
            self.assertEqual(len(pipeline._SCOPE_TO_EVAL_TARGETS[scope]), 1)
        # meta/global 是全量（多 target）
        self.assertGreater(len(pipeline._SCOPE_TO_EVAL_TARGETS["meta"]), 1)
        self.assertGreater(len(pipeline._SCOPE_TO_EVAL_TARGETS["global"]), 1)

    def test_get_trace_avg_score_filtered_by_target(self) -> None:
        """_get_trace_avg_score_filtered 按 target 过滤分数。"""
        from app.improvement import pipeline
        trace_id = "test-trace-1"
        self._seed_run(trace_id)
        # 写入不同 target 的分数
        for target, score in [("writing", 0.8), ("storybuilding", 0.5), ("content", 0.9)]:
            self.db.execute(
                "INSERT INTO evaluation_scores (trace_id, layer, target, metric, score, scored_at) "
                "VALUES (?, 'subagent', ?, 'quality', ?, 'now')",
                (trace_id, target, score),
            )
        # 不过滤：全部均分
        all_avg = pipeline._get_trace_avg_score_filtered(trace_id, None)
        self.assertAlmostEqual(all_avg, (0.8 + 0.5 + 0.9) / 3, places=3)
        # 只过滤 writing：(0.8,)
        writing_only = pipeline._get_trace_avg_score_filtered(trace_id, ["writing"])
        self.assertAlmostEqual(writing_only, 0.8, places=3)
        # writing + storybuilding
        two = pipeline._get_trace_avg_score_filtered(trace_id, ["writing", "storybuilding"])
        self.assertAlmostEqual(two, (0.8 + 0.5) / 2, places=3)

    def test_get_trace_avg_score_filtered_no_match(self) -> None:
        """target 无匹配返回 None。"""
        from app.improvement import pipeline
        trace_id = "test-trace-2"
        self._seed_run(trace_id)
        self.db.execute(
            "INSERT INTO evaluation_scores (trace_id, layer, target, metric, score, scored_at) "
            "VALUES (?, 'subagent', 'writing', 'quality', 0.7, 'now')",
            (trace_id,),
        )
        self.assertIsNone(pipeline._get_trace_avg_score_filtered(trace_id, ["interview"]))

    def _seed_run(self, trace_id: str) -> None:
        """建 run 行满足 evaluation_scores 的外键约束。"""
        self.db.execute(
            "INSERT INTO runs (trace_id, workspace_id, status, ingested_at) "
            "VALUES (?, 'ws-test', 'completed', 'now')",
            (trace_id,),
        )

    # ── manifest_publisher: approve + notify（降级）─────────

    def test_approve_and_publish_flow(self) -> None:
        """approve_and_publish：surface approved + manifest 发布 + 通知（降级）。"""
        from app.improvement import surface_repo, manifest_publisher, manifest_repo
        # executor_url 未配置 → notify 返回 False（降级），但流程不中断
        v2 = surface_repo.create_version(
            "prompt", "writing_system", "writing", "改进的 prompt", status="draft",
        )
        result = manifest_publisher.approve_and_publish(v2["id"])
        self.assertIsNotNone(result)
        self.assertEqual(result["surface_version_id"], v2["id"])
        self.assertIsNotNone(result["manifest_version"])
        # notified=False（executor_url 未配置），但流程成功
        self.assertFalse(result["notified"])
        # surface 已 approved
        self.assertEqual(surface_repo.get_version_by_id(v2["id"])["status"], "approved")
        # manifest 已发布
        self.assertIsNotNone(manifest_repo.get_production_manifest())

    def test_approve_and_publish_nonexistent_returns_none(self) -> None:
        """不存在的 surface 版本返回 None。"""
        from app.improvement import manifest_publisher
        self.assertIsNone(manifest_publisher.approve_and_publish(99999))

    def test_notify_executor_no_url_returns_false(self) -> None:
        """executor_url 未配置时 notify 返回 False（不报错）。"""
        from app.improvement import manifest_publisher
        self.assertFalse(manifest_publisher.notify_executor(1))


if __name__ == "__main__":
    unittest.main()
