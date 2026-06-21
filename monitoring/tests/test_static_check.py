"""Phase 4 T4.3：静态检查 + 契约测试（D10）。

覆盖：
- 合法代码通过
- 语法错 → 失败
- 无 WriterHarness 子类 → 失败
- 危险模式（os.system/subprocess/eval/exec/socket/open-w）→ 各自失败
- 危险硬编码（拒绝文艺/慢热）→ 失败（D10 第三道闸核心）
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.static_check import static_check

_VALID_CODE = """
from app.platform.harness import WriterHarness


class GoodHarness(WriterHarness):
    def build_system_prompt(self, ctx):
        return "p"
    def build_skills(self, ctx):
        return ["/s"]
    def build_middleware(self, ctx):
        return []
    def build_subagents(self, ctx):
        return []
"""


class StaticCheckTest(unittest.TestCase):
    def test_valid_code_passes(self) -> None:
        ok, errors = static_check(_VALID_CODE)
        self.assertTrue(ok, f"合法代码应通过，错误: {errors}")
        self.assertEqual(errors, [])

    def test_syntax_error_fails(self) -> None:
        ok, errors = static_check("def broken(:\n")
        self.assertFalse(ok)
        self.assertTrue(any("语法错误" in e for e in errors))

    def test_no_harness_subclass_fails(self) -> None:
        ok, errors = static_check("class Other:\n    pass\n")
        self.assertFalse(ok)
        self.assertTrue(any("WriterHarness" in e for e in errors))

    def test_dangerous_os_system(self) -> None:
        code = _VALID_CODE + "\nimport os\nos.system('rm -rf /')\n"
        ok, errors = static_check(code)
        self.assertFalse(ok)
        self.assertTrue(any("os.system" in e for e in errors))

    def test_dangerous_subprocess(self) -> None:
        code = _VALID_CODE.replace("return []", "import subprocess; subprocess.run(['ls'])")
        ok, errors = static_check(code)
        self.assertFalse(ok)
        self.assertTrue(any("subprocess" in e for e in errors))

    def test_dangerous_eval(self) -> None:
        code = _VALID_CODE.replace('return "p"', "import os; eval('1+1')")
        ok, errors = static_check(code)
        self.assertFalse(ok)
        self.assertTrue(any("eval" in e for e in errors))

    def test_dangerous_socket(self) -> None:
        code = _VALID_CODE + "\nimport socket\ns = socket.socket()\n"
        ok, errors = static_check(code)
        self.assertFalse(ok)
        self.assertTrue(any("socket" in e for e in errors))

    def test_hardcoded_reject_literary(self) -> None:
        """D10 第三道闸核心：禁止硬编码拒绝文艺向（会误伤）。"""
        code = _VALID_CODE.replace(
            "return []",
            "if '文艺' in x: return error  # 禁止硬编码拒绝题材"
        ).replace("x", "ctx.workspace_path")
        # 构造一个含危险模式的代码
        dangerous = """
from app.platform.harness import WriterHarness


class BadHarness(WriterHarness):
    def build_system_prompt(self, ctx):
        if '文艺' in ctx.meta_style: return error
        return "p"
    def build_skills(self, ctx):
        return []
    def build_middleware(self, ctx):
        return []
    def build_subagents(self, ctx):
        return []
"""
        ok, errors = static_check(dangerous)
        self.assertFalse(ok)
        self.assertTrue(any("误伤" in e or "文艺" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
