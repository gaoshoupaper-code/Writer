"""CON-002 进化禁写评估器/契约代码测试（AC-012 / AC-019）。

验证物理隔离的纵深防御：
  - AC-012：edit_source 试图改 eval_agent/ 代码 → 拒绝 + 记录违规。
  - AC-019：edit_source 试图改 contracts/ 契约文件 → 拒绝。

主隔离靠 FilesystemBackend(root_dir=harness_work_dir, virtual_mode=True)，
eval_agent/contracts 都在 root 之外物理不可达。本测试固化显式 forbidden-prefix 纵深防御。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evolve.agent.tools.writers import (  # noqa: E402
    _FORBIDDEN_EDIT_PREFIXES,
    _SAFE_NAME,
    _sanitize_name,
)


class Con002ForbiddenPrefixTest(unittest.TestCase):
    """AC-012 / AC-019：edit_source forbidden-prefix 纵深防御。"""

    def test_forbidden_prefixes_include_eval_and_contracts(self):
        """forbidden-prefix 清单含 eval_agent/ 和 contracts/。"""
        prefixes_str = " ".join(_FORBIDDEN_EDIT_PREFIXES)
        self.assertIn("eval_agent/", prefixes_str)
        self.assertIn("contracts/", prefixes_str)

    def test_forbidden_prefix_constant_is_frozen(self):
        """_FORBIDDEN_EDIT_PREFIXES 是 frozenset/tuple（防运行时篡改）。"""
        # 声明形式（tuple），固化不可变
        self.assertIsInstance(_FORBIDDEN_EDIT_PREFIXES, tuple)


class Con002SanitizeNameTest(unittest.TestCase):
    """_sanitize_name 拒绝路径分隔符（专用写工具的隔离层）。"""

    def test_rejects_path_separator(self):
        """专用写工具的 name 不允许路径分隔符（/ 或 \\）。

        注意：_SAFE_NAME 允许点号，所以纯 ".." 字符串能过 name 校验——
        但它会被 FilesystemBackend 的 root_dir 虚拟化在解析阶段拦截。
        _sanitize_name 这层只负责拦"含分隔符的路径穿越"。
        """
        for bad in ("a/b", "../x", "eval_agent/foo", "a\\b"):
            with self.assertRaises(ValueError, msg=f"应拒绝 {bad}"):
                _sanitize_name(bad)

    def test_accepts_clean_name(self):
        """干净文件名通过。"""
        self.assertEqual(_sanitize_name("pacing"), "pacing")
        self.assertEqual(_sanitize_name("pacing", ".py"), "pacing.py")
        self.assertEqual(_sanitize_name("writing_system", ".md"), "writing_system.md")


class Con002EditSourceLogicTest(unittest.TestCase):
    """edit_source 的 forbidden-prefix 检查逻辑（通过源码 AST + 直接调用）。

    edit_source 是 @tool 装饰的闭包，直接调用需要 mock backend。这里通过源码固化
    forbidden-prefix 检查存在 + 用 mock backend 直接调一次验证拒绝行为。
    """
    def test_edit_source_source_has_forbidden_check(self):
        """report.py 源码含 forbidden-prefix 检查（防回退）。"""
        writers_path = Path(__file__).resolve().parent.parent / "app" / "evolve" / "agent" / "tools" / "writers.py"
        source = writers_path.read_text(encoding="utf-8")
        self.assertIn("_FORBIDDEN_EDIT_PREFIXES", source)
        self.assertIn("CON-002", source)
        self.assertIn("reward hacking", source)

    def test_edit_source_rejects_eval_agent_path(self):
        """AC-012：edit_source 改 eval_agent/foo.py → 拒绝。"""
        from types import SimpleNamespace
        from app.evolve.ctx import EvolveContext, set_tool_context
        from app.evolve.agent.tools.writers import make_writer_tools

        ctx = EvolveContext("sess-con002")
        set_tool_context(ctx)

        # mock backend：即便 backend 不拒绝，edit_source 的 forbidden-prefix 应先拒
        fake_backend = SimpleNamespace(
            edit=lambda *a, **kw: SimpleNamespace(error=None, occurrences=1),
        )
        tools = make_writer_tools(fake_backend)
        edit_source = next(t for t in tools if t.name == "edit_source")

        result = edit_source.invoke({
            "file_path": "eval_agent/scoring.py",
            "old_string": "x", "new_string": "y",
        })
        self.assertIn("拒绝", result)
        self.assertIn("CON-002", result)
        self.assertIn("eval_agent", result)

    def test_edit_source_rejects_contracts_path(self):
        """AC-019：edit_source 改 contracts/foo.yaml → 拒绝。"""
        from types import SimpleNamespace
        from app.evolve.ctx import EvolveContext, set_tool_context
        from app.evolve.agent.tools.writers import make_writer_tools

        ctx = EvolveContext("sess-con002")
        set_tool_context(ctx)

        fake_backend = SimpleNamespace(
            edit=lambda *a, **kw: SimpleNamespace(error=None, occurrences=1),
        )
        tools = make_writer_tools(fake_backend)
        edit_source = next(t for t in tools if t.name == "edit_source")

        result = edit_source.invoke({
            "file_path": "contracts/structural.yaml",
            "old_string": "x", "new_string": "y",
        })
        self.assertIn("拒绝", result)
        self.assertIn("contracts", result)

    def test_edit_source_allows_harness_path(self):
        """合法 harness 路径 → 不被 forbidden-prefix 拦（透到 backend）。"""
        from types import SimpleNamespace
        from app.evolve.ctx import EvolveContext, set_tool_context
        from app.evolve.agent.tools.writers import make_writer_tools

        ctx = EvolveContext("sess-con002")
        set_tool_context(ctx)

        called = {}
        def fake_edit(path, old, new):
            called["path"] = path
            return SimpleNamespace(error=None, occurrences=1)
        fake_backend = SimpleNamespace(edit=fake_edit)
        tools = make_writer_tools(fake_backend)
        edit_source = next(t for t in tools if t.name == "edit_source")

        result = edit_source.invoke({
            "file_path": "middleware/pacing.py",
            "old_string": "x", "new_string": "y",
        })
        self.assertIn("已编辑", result, f"合法路径应通过，实际：{result}")
        self.assertEqual(called["path"], "/middleware/pacing.py")


if __name__ == "__main__":
    unittest.main()
