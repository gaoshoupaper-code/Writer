"""Phase 6 切换验证测试（T5.1 分发逻辑 + 降级链）。

测执行端的：三档开关分发 + manifest 拉取失败降级。
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSwitchDispatch:
    """三档开关分发逻辑测试（writer_use_manifest > writer_use_harness > 旧直接）。"""

    def test_settings_has_manifest_fields(self) -> None:
        """settings 包含 Phase 6 新增字段。"""
        from app.platform.core.settings import get_settings
        s = get_settings()
        assert hasattr(s, "writer_use_manifest")
        assert hasattr(s, "writer_use_harness")
        assert hasattr(s, "evolution_url")
        assert hasattr(s, "manifest_cache_dir")
        # 默认全 False（不影响生产）
        assert s.writer_use_manifest is False
        assert s.writer_use_harness is False

    def test_manifest_priority_over_harness(self) -> None:
        """writer_use_manifest=True 时走 manifest 路径（即使 harness 也开）。"""
        from app.platform.core.settings import get_settings
        from app.domains.writing.meta.agent import MetaAgentService
        s = get_settings()
        with mock.patch.object(s, "writer_use_manifest", True), \
             mock.patch.object(s, "writer_use_harness", True):
            svc = MetaAgentService.__new__(MetaAgentService)
            svc.settings = s
            with mock.patch.object(svc, "_assemble_via_manifest") as m_mock, \
                 mock.patch.object(svc, "_assemble_via_harness") as h_mock:
                svc._agent_for_workspace(Path("/tmp"), None, None)
                m_mock.assert_called_once()
                h_mock.assert_not_called()

    def test_harness_when_manifest_off(self) -> None:
        """writer_use_manifest=False + writer_use_harness=True 时走 harness。"""
        from app.platform.core.settings import get_settings
        from app.domains.writing.meta.agent import MetaAgentService
        s = get_settings()
        with mock.patch.object(s, "writer_use_manifest", False), \
             mock.patch.object(s, "writer_use_harness", True):
            svc = MetaAgentService.__new__(MetaAgentService)
            svc.settings = s
            with mock.patch.object(svc, "_assemble_via_manifest") as m_mock, \
                 mock.patch.object(svc, "_assemble_via_harness") as h_mock:
                svc._agent_for_workspace(Path("/tmp"), None, None)
                m_mock.assert_not_called()
                h_mock.assert_called_once()

    def test_legacy_when_both_off(self) -> None:
        """两档都关时不走 manifest/harness 新路径（走旧直接装配）。"""
        from app.platform.core.settings import get_settings
        from app.domains.writing.meta.agent import MetaAgentService
        s = get_settings()
        with mock.patch.object(s, "writer_use_manifest", False), \
             mock.patch.object(s, "writer_use_harness", False):
            svc = MetaAgentService.__new__(MetaAgentService)
            svc.settings = s
            svc.checkpointer = None
            # 旧路径会真跑装配（依赖 model/prompt 等），这里让它早抛错，
            # 只验证不走 manifest/harness 方法即可
            with mock.patch.object(svc, "_assemble_via_manifest") as m_mock, \
                 mock.patch.object(svc, "_assemble_via_harness") as h_mock:
                try:
                    svc._agent_for_workspace(Path("/tmp"), None, None)
                except Exception:
                    pass  # 旧路径依赖多，抛任何错都行
                m_mock.assert_not_called()
                h_mock.assert_not_called()

    def test_manifest_loader_reads_settings(self) -> None:
        """manifest_loader.get_loader() 从 settings 读 evolution_url + cache_dir。"""
        from app.platform.harness.manifest_loader import get_loader
        from app.platform.core.settings import get_settings
        # 重置单例确保读最新 settings
        import app.platform.harness.manifest_loader as ml
        ml._loader = None
        loader = get_loader()
        s = get_settings()
        assert loader._evolution_url == s.evolution_url.rstrip("/")


class TestFallbackChain:
    """manifest 装配降级链测试。"""

    def test_assemble_via_manifest_falls_back_when_fetch_fails(self) -> None:
        """manifest 拉取失败时降级到 harness 路径（设计 D5 + T4.4）。"""
        from app.domains.writing.meta.agent import MetaAgentService
        from app.platform.core.settings import get_settings
        svc = MetaAgentService.__new__(MetaAgentService)
        svc.settings = get_settings()
        svc.checkpointer = None

        with mock.patch(
            "app.platform.harness.manifest_loader.get_loader"
        ) as gl_mock:
            loader_mock = mock.MagicMock()
            loader_mock.fetch_production.return_value = None
            gl_mock.return_value = loader_mock
            with mock.patch.object(svc, "_assemble_via_harness") as h_mock:
                h_mock.return_value = "fallback-agent"
                result = svc._assemble_via_manifest(Path("/tmp"), None, None)
                h_mock.assert_called_once()
                assert result == "fallback-agent"
