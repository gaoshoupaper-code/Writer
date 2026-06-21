"""Phase 4 T4.2：沙箱验证测试。

覆盖：
- validate_candidate：合法 harness 通过
- 加载失败（语法错/无子类）→ 失败
- 契约方法返回类型错误 → 失败
- smoke_test_generation 占位行为
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.worker.sandbox import validate_candidate, smoke_test_generation


_VALID_CODE = '''
from app.platform.harness import WriterHarness


class GoodHarness(WriterHarness):
    def build_system_prompt(self, ctx):
        return "prompt"
    def build_skills(self, ctx):
        return ["/skills/a"]
    def build_middleware(self, ctx):
        return []
    def build_subagents(self, ctx):
        return []
'''


def _write(tmp_path, code, name="harness.py"):
    p = tmp_path / name
    p.write_text(code, encoding="utf-8")
    return p


class TestValidateCandidate:
    def test_valid_harness_passes(self, tmp_path) -> None:
        path = _write(tmp_path, _VALID_CODE)
        ok, errors = validate_candidate(path)
        assert ok, f"合法应通过: {errors}"

    def test_syntax_error_fails(self, tmp_path) -> None:
        path = _write(tmp_path, "def broken(:\n")
        ok, errors = validate_candidate(path)
        assert not ok
        assert any("加载失败" in e or "语法" in e for e in errors)

    def test_no_subclass_fails(self, tmp_path) -> None:
        path = _write(tmp_path, "class Other:\n    pass\n")
        ok, errors = validate_candidate(path)
        assert not ok

    def test_middleware_wrong_return_type(self, tmp_path) -> None:
        """build_middleware 返回非 list → C2 失败。"""
        bad = _VALID_CODE.replace("return []", "return 'not a list'")
        path = _write(tmp_path, bad)
        ok, errors = validate_candidate(path)
        assert not ok
        assert any("build_middleware" in e for e in errors)

    def test_skills_wrong_element_type(self, tmp_path) -> None:
        """build_skills 返回非 str 元素 → C3 失败。"""
        bad = _VALID_CODE.replace('return ["/skills/a"]', "return [123]")
        path = _write(tmp_path, bad)
        ok, errors = validate_candidate(path)
        assert not ok
        assert any("build_skills" in e for e in errors)

    def test_subagents_not_list(self, tmp_path) -> None:
        bad = _VALID_CODE.replace("return []", "return None", 1)
        # 注意：replace 从前到后，第一个 return [] 是 middleware，要精确改 subagents
        bad = _VALID_CODE.replace(
            "    def build_subagents(self, ctx):\n        return []",
            "    def build_subagents(self, ctx):\n        return None",
        )
        path = _write(tmp_path, bad)
        ok, errors = validate_candidate(path)
        assert not ok
        assert any("build_subagents" in e for e in errors)


class TestSmokeTestGeneration:
    def test_returns_ok_for_valid(self, tmp_path) -> None:
        path = _write(tmp_path, _VALID_CODE)
        ok, msg = smoke_test_generation(path, "test request")
        assert ok

    def test_returns_fail_for_invalid(self, tmp_path) -> None:
        path = _write(tmp_path, "def broken(:\n")
        ok, msg = smoke_test_generation(path, "test")
        assert not ok
