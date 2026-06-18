"""ImageAgentService — 文生图优化 Agent 服务（Phase 3，DD3）。

继承 platform BaseAgentService，组装文生图 agent：
- DeepAgent 外壳（复用 checkpoint/SSE/trace/HITL）
- 工具：generate_images / analyze_image / ask_user / persist_skill
- Skill：image-workflow（闭环流程）+ 用户私有 Skill（DD7b，按需加载）

闭环（D4/D5/D6）由 system prompt + skill 引导，agent 自主编排。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.core.settings import Settings
from app.db import get_database
from app.domains.image.store import ImageArtifactStore
from app.domains.image.tools import (
    build_analyze_image_tool,
    build_generate_images_tool,
    build_persist_skill_tool,
)
from app.platform.agent.base_service import BaseAgentService
from app.platform.agent.middleware import (
    WRITING_WRITE_PATTERNS,
    ErrorRecoveryMiddleware,
    FilesystemPathGuardMiddleware,
    FileWriteSerializeMiddleware,
    TraceMiddleware,
)
from app.platform.agent.runtime import (
    FilesystemBackend,
    compose_skills_backend,
    create_deep_agent,
)
from app.platform.tools import build_ask_user_tool

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# image domain 的写路径白名单（DD6a：参数化注入）
import re
IMAGE_WRITE_PATTERNS = (
    re.compile(r"^/images/[^/]+\.(png|jpg|jpeg|webp)$"),
    re.compile(r"^/skills/[^/]+/SKILL\.md$"),
)


class ImageAgentService(BaseAgentService):
    """文生图优化 Agent 服务。

    继承 BaseAgentService 的通用能力（model/checkpointer 解析、checkpoint 读写），
    在此基础上组装 image domain 专属的 agent + 工具。
    """

    def __init__(
        self,
        settings: Settings,
        workspace_root: Path,
        trace_recorder: Any,
        checkpointer: BaseCheckpointSaver,
    ) -> None:
        super().__init__(settings, workspace_root, trace_recorder, checkpointer)

    def _load_system_prompt(self) -> str:
        return (PROMPTS_DIR / "system.md").read_text(encoding="utf-8").strip()

    def _build_agent(
        self,
        workspace_path: Path,
        trace_id: str | None,
        workspace_id: str,
        owner_id: str,
        *,
        model=None,
        checkpointer=None,
        selected_skill_ids: list[str] | None = None,
    ):
        """构建文生图 agent（DD3：DeepAgent 外壳 + 工具）。

        Args:
            selected_skill_ids: 用户选中的私有 Skill（D9 Agent 推荐 + 用户确认）。
                                 None/空 = 纯冷启动（D20），不加载私有 Skill。
        """
        if model is None:
            model = self._resolve_model(owner_id)
        if checkpointer is None:
            checkpointer = self.checkpointer

        store = ImageArtifactStore(get_database())

        # 工具（闭包捕获 workspace 上下文）
        tools = [
            build_generate_images_tool(
                store, self.settings, workspace_path, workspace_id, owner_id,
            ),
            build_analyze_image_tool(store, self.settings, workspace_path, owner_id),
            build_ask_user_tool(),
            build_persist_skill_tool(owner_id),
        ]

        # middleware：platform 通用 + image 白名单（DD6a）
        middleware = [
            ErrorRecoveryMiddleware(),
            FilesystemPathGuardMiddleware(workspace_path, allowed_patterns=IMAGE_WRITE_PATTERNS),
            FileWriteSerializeMiddleware(),
        ]
        if trace_id:
            middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, "image-agent"))

        # backend：workspace + image-workflow skill + 用户私有 skill
        backend = self._backend_for_workspace(workspace_path)
        skill_paths = [str(PROMPTS_DIR.parent / "skills")]  # image-workflow skill
        if selected_skill_ids:
            from app.platform.skills.loader import resolve_owner_skills
            skill_paths.extend(resolve_owner_skills(owner_id, selected_skill_ids))
        effective_backend, skill_sources = compose_skills_backend(backend, skill_paths)

        return create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=self._load_system_prompt(),
            middleware=middleware,
            backend=effective_backend,
            checkpointer=checkpointer,
            skills=skill_sources,
        )

    def _build_user_prompt(self, user_need: str, thread_id: str) -> str:
        """构造发给 image-agent 的用户输入。"""
        return (
            f"用户想生成的图片：\n{user_need}\n\n"
            f"当前 session：{thread_id}\n"
            "请先读取 image-workflow Skill，然后按闭环流程执行："
            "优化 3 版提示词 → 生图 → 自评 → 请用户打分。"
        )


__all__ = ["ImageAgentService"]
