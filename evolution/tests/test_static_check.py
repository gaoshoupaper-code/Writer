"""surface 校验测试（Phase 6 surface 体系）。

覆盖 validate_text_surface / validate_json_surface / validate_python_surface：
- A 类（纯文本）：空/超长失败，正常通过
- B 类（JSON）：非法 JSON 失败，合法通过
- C 类（受限 Python）：语法错/危险模式（C4）/误伤硬编码（C5）/无 state_schema 失败，合法 middleware 通过
- VALIDATOR_MAP 分发正确
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.improvement import static_check


# C 类合法 middleware（带 state_schema）
_VALID_C_CODE = """
from langchain.agents.middleware.types import AgentMiddleware


class GoalMiddleware(AgentMiddleware):
    state_schema = GoalState

    def before_model(self, state):
        return state
"""


class ValidateTextSurfaceTest(unittest.TestCase):
    def test_valid_text_passes(self) -> None:
        ok, errors = static_check.validate_text_surface("正常 prompt 内容", {})
        self.assertTrue(ok)

    def test_empty_text_fails(self) -> None:
        ok, errors = static_check.validate_text_surface("", {})
        self.assertFalse(ok)
        self.assertTrue(any("空" in e for e in errors))

    def test_too_long_fails(self) -> None:
        ok, errors = static_check.validate_text_surface("x" * 50001, {})
        self.assertFalse(ok)
        self.assertTrue(any("超长" in e for e in errors))


class ValidateJsonSurfaceTest(unittest.TestCase):
    def test_valid_json_passes(self) -> None:
        ok, errors = static_check.validate_json_surface('{"max_new_lines": 3}', {})
        self.assertTrue(ok)

    def test_invalid_json_fails(self) -> None:
        ok, errors = static_check.validate_json_surface("{not json", {})
        self.assertFalse(ok)
        self.assertTrue(any("JSON 解析失败" in e for e in errors))


class ValidatePythonSurfaceTest(unittest.TestCase):
    def test_valid_middleware_passes(self) -> None:
        ok, errors = static_check.validate_python_surface(_VALID_C_CODE, {})
        self.assertTrue(ok, f"合法 middleware 应通过，错误: {errors}")

    def test_syntax_error_fails(self) -> None:
        ok, errors = static_check.validate_python_surface("def broken(:\n", {})
        self.assertFalse(ok)
        self.assertTrue(any("语法错误" in e for e in errors))

    def test_dangerous_os_system(self) -> None:
        code = _VALID_C_CODE + "\nimport os\nos.system('rm -rf /')\n"
        ok, errors = static_check.validate_python_surface(code, {})
        self.assertFalse(ok)
        self.assertTrue(any("os.system" in e for e in errors))

    def test_dangerous_eval(self) -> None:
        code = _VALID_C_CODE.replace("return state", "eval('1+1')")
        ok, errors = static_check.validate_python_surface(code, {})
        self.assertFalse(ok)
        self.assertTrue(any("eval" in e for e in errors))

    def test_no_state_schema_fails(self) -> None:
        """C 类契约：middleware 必须有 state_schema 属性。"""
        code = """
class GoalMiddleware(AgentMiddleware):
    def before_model(self, state):
        return state
"""
        ok, errors = static_check.validate_python_surface(code, {})
        self.assertFalse(ok)
        self.assertTrue(any("state_schema" in e for e in errors))


class ValidatorMapTest(unittest.TestCase):
    def test_map_covers_all_types(self) -> None:
        """VALIDATOR_MAP 覆盖 contracts.REGISTRY 的所有 surface_type。"""
        from contracts import surface_types
        for st in surface_types.REGISTRY:
            self.assertIn(st, static_check.VALIDATOR_MAP, f"{st} 缺 validator")

    def test_validate_surface_dispatch(self) -> None:
        """validate_surface 按 surface_type 分发到正确 validator。"""
        # A 类分发到 text validator（空内容失败）
        ok, errors = static_check.validate_surface("prompt", "")
        self.assertFalse(ok)
        # B 类分发到 json validator（非法 JSON 失败）
        ok, errors = static_check.validate_surface("middleware_params", "{bad")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
