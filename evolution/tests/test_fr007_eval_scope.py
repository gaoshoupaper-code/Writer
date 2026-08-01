"""FR-007 判评分离测试（AC-008）。

验证：
  - _VALID_SCOPES 含 eval。
  - build_agent_model 接受 scope 参数。
  - eval scope 未配置 → 降级用 evolution scope + 警告（EDGE-005）。
  - 同家族 → 警告（不阻塞）。

设计依据：.claude/md/20260801_192157_进化信息可见性与评估漏判.md FR-007 / DEC-007 / EVD-004
"""
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"
# LlmConfigsRepository.create 加密 api_key 需要 master key
os.environ["EVOLUTION_MASTER_KEY"] = "a" * 64

import app.core.db as db  # noqa: E402
from app.common.model_factory import build_agent_model, _model_family  # noqa: E402
from app.config.api import _VALID_SCOPES, _normalize_scope  # noqa: E402
from app.core.settings import settings  # noqa: E402


def setUpModule() -> None:
    db._conn = None
    db.init_db()
    # settings 是导入期单例，跨测试模块共享。LlmConfigsRepository.create 走加密路径
    # 需要 master key——这里直接在单例上设值，并清 db 的 key 缓存，避免被先跑的模块污染。
    settings.evolution_master_key = settings.evolution_master_key or "a" * 64
    db._master_key_cache = None


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


def _seed_config(scope, model="deepseek-chat", is_active=True):
    """插入一条 llm_configs 配置（通过 Repository 走加密路径）。"""
    cfg_id = db.LlmConfigsRepository.create(
        name=f"{scope}-cfg-{model}", api_key="sk-test", base_url="http://x",
        model=model, scope=scope,
    )
    if not is_active:
        db.execute("UPDATE llm_configs SET is_active=0 WHERE id=?", (cfg_id,))
    return cfg_id


class EvalScopeConfigTest(unittest.TestCase):
    """AC-008：判评分离配置校验。"""

    def setUp(self):
        """每个测试前清 master_key 缓存（防跨测试模块污染）。"""
        db._master_key_cache = None

    def test_valid_scopes_includes_eval(self):
        """_VALID_SCOPES 含 eval（FR-007 新增）。"""
        self.assertIn("eval", _VALID_SCOPES)
        self.assertIn("evolution", _VALID_SCOPES)
        self.assertIn("executor", _VALID_SCOPES)

    def test_normalize_accepts_eval_scope(self):
        """_normalize_scope 接受 eval。"""
        self.assertEqual(_normalize_scope("eval"), "eval")
        # 默认仍 evolution（向后兼容）
        self.assertEqual(_normalize_scope(None), "evolution")
        self.assertEqual(_normalize_scope(""), "evolution")

    def test_normalize_rejects_invalid_scope(self):
        """非法 scope → 400。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            _normalize_scope("invalid_scope")

    def test_build_agent_model_accepts_scope_param(self):
        """build_agent_model signature 含 scope 参数（默认 evolution）。"""
        import inspect
        sig = inspect.signature(build_agent_model)
        self.assertIn("scope", sig.parameters)
        self.assertEqual(sig.parameters["scope"].default, "evolution")

    def test_ac008_eval_scope_configured_uses_eval(self):
        """AC-008 第一档：eval scope 配置了异家族模型 → 正常用 eval scope。"""
        _seed_config("evolution", model="deepseek-chat")
        _seed_config("eval", model="glm-4-plus")
        # 不应抛
        model = build_agent_model(temperature=0.2, scope="eval")
        self.assertEqual(model.model_name, "glm-4-plus")

    def test_ac008_eval_scope_missing_falls_back_to_evolution(self):
        """AC-008 第三档：eval scope 未配置 → 降级用 evolution scope + 警告（EDGE-005）。"""
        _seed_config("evolution", model="deepseek-chat")
        # 清掉 eval scope（确保无激活配置）
        db.execute("DELETE FROM llm_configs WHERE scope = ?", ("eval",))
        with self.assertLogs("evolution.common.model_factory", level="WARNING") as cm:
            model = build_agent_model(temperature=0.2, scope="eval")
        # 实际用了 evolution scope 的模型
        self.assertEqual(model.model_name, "deepseek-chat")
        # 警告含 PLS 风险提示
        logged = "\n".join(cm.output)
        self.assertIn("eval scope 未配置", logged)
        self.assertIn("PLS", logged)

    def test_ac008_same_family_warns(self):
        """AC-008 第二档：eval 配了但与 evolution 同家族 → 警告（不阻塞）。"""
        _seed_config("evolution", model="deepseek-chat")
        _seed_config("eval", model="deepseek-coder")  # 同 deepseek 家族
        with self.assertLogs("evolution.common.model_factory", level="WARNING") as cm:
            model = build_agent_model(temperature=0.2, scope="eval")
        # 仍正常返回（不阻塞）
        self.assertEqual(model.model_name, "deepseek-coder")
        # 警告含 PLS 风险
        logged = "\n".join(cm.output)
        self.assertIn("PLS", logged)

    def test_model_family_detection(self):
        """_model_family 正确判家族。"""
        self.assertEqual(_model_family("deepseek-chat"), "deepseek")
        self.assertEqual(_model_family("glm-4-plus"), "glm")
        self.assertEqual(_model_family("openai:gpt-4o-mini"), "gpt")
        self.assertEqual(_model_family("claude-3-opus"), "claude")
        self.assertNotEqual(_model_family("deepseek-chat"), _model_family("glm-4"))


if __name__ == "__main__":
    unittest.main()
