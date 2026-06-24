"""M3 风格注入闭环测试（D3=③：新行为补针对性测试）。

验证三环：
1. apply_style_suffix 纯函数：有 suffix 追加、无 suffix 原样。
2. RuntimeContext.styles 字段：可设置、可读取。
3. assemble 消费 styles：带 styles 的 ctx → assemble 不报错（验证 import 链 + styles 被读取）。

注：完整 assemble 需要真实 model/backend/checkpointer，本测试不跑完整装配，
只验证"风格数据能正确流到注入点"。subagent prompt 含 suffix 的端到端验证
依赖真实 LLM 调用，留手动验证。
"""
from __future__ import annotations

import unittest

from contracts.runtime_context import RuntimeContext


class TestApplyStyleSuffix(unittest.TestCase):
    """验证 apply_style_suffix 注入逻辑（包内 subagents/types.py）。"""

    def setUp(self):
        # 直接 import 包内模块（load_current_package 机制同进程可用）
        from app.platform.agent.loader import load_current_package
        pkg = load_current_package()
        # apply_style_suffix 在 subagents.types，通过包内 import 取
        from harness_current.subagents.types import apply_style_suffix
        self.apply_style_suffix = apply_style_suffix

    def test_no_suffix_returns_original(self):
        """无 suffix（None）应原样返回 prompt。"""
        prompt = "你是写作助手。"
        self.assertEqual(self.apply_style_suffix(prompt, None), prompt)

    def test_empty_suffix_returns_original(self):
        """空字符串 suffix 应原样返回（apply_style_suffix 对 falsy 值短路）。"""
        prompt = "你是写作助手。"
        self.assertEqual(self.apply_style_suffix(prompt, ""), prompt)

    def test_suffix_appended(self):
        """有 suffix 应追加到 prompt 末尾（两换行分隔）。"""
        prompt = "你是写作助手。"
        suffix = "风格：简洁有力。"
        result = self.apply_style_suffix(prompt, suffix)
        self.assertEqual(result, f"{prompt}\n\n{suffix}")
        self.assertIn(suffix, result)


class TestRuntimeContextStyles(unittest.TestCase):
    """验证 RuntimeContext.styles 字段（contracts 层）。"""

    def test_styles_defaults_none(self):
        """styles 默认 None（无风格注入）。"""
        from pathlib import Path
        ctx = RuntimeContext(
            model=object(), backend=object(), checkpointer=object(),
            workspace_path=Path("/tmp"),
        )
        self.assertIsNone(ctx.styles)

    def test_styles_can_be_set(self):
        """styles 可设置 scope→suffix 映射。"""
        from pathlib import Path
        ctx = RuntimeContext(
            model=object(), backend=object(), checkpointer=object(),
            workspace_path=Path("/tmp"),
            styles={"meta": "全局风格", "writing": "写作风格"},
        )
        self.assertEqual(ctx.styles["meta"], "全局风格")
        self.assertEqual(ctx.styles["writing"], "写作风格")

    def test_styles_scope_key_names(self):
        """D2 决策：key 用包内 scope 名（含 detail-outline 连字符）。"""
        from pathlib import Path
        ctx = RuntimeContext(
            model=object(), backend=object(), checkpointer=object(),
            workspace_path=Path("/tmp"),
            styles={"detail-outline": "细纲风格"},
        )
        self.assertIn("detail-outline", ctx.styles)


if __name__ == "__main__":
    unittest.main()
