"""CharacterService — 角色生成服务（面向 API 端点）。

封装了角色生成的完整生命周期：
1. 从配置构建模型和后端
2. 组装中间件（路径守卫、追踪等）
3. 构建用户提示词
4. 调用代理生成角色
5. 验证工作区文件并返回结果

支持 live 模式（真实代理调用）和 mock 模式（模拟返回，用于测试）。
"""

from __future__ import annotations

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.writer.middleware import FilesystemPathGuardMiddleware, TraceCallbackHandler, TraceMiddleware
from app.writer.models import build_writer_model

# 独立于 agents/character.py 的 prompt 路径（该 agent 已被 storybuilding 替代）
_CHARACTER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "character_system.md"
from app.core.settings import Settings
from app.writer.trace import TraceRecorder
from app.schemas.character import (
    CharacterGenerateRequest,
    CharacterGenerateResponse,
)
from app.schemas.screenplay import ThreadSummary


class CharacterService:
    """角色生成服务。

    封装了角色生成的完整生命周期：
    1. 从配置构建模型和后端
    2. 组装中间件（路径守卫、追踪等）
    3. 构建用户提示词
    4. 调用代理生成角色
    5. 验证工作区文件并返回结果

    支持 live 模式（真实代理调用）和 mock 模式（模拟返回，用于测试）。
    """

    def __init__(self, settings: Settings, workspace_root: Path, trace_recorder: TraceRecorder, checkpointer: BaseCheckpointSaver) -> None:
        """
        Args:
            settings:       应用配置（包含模型、模式等设置）
            workspace_root: 工作区根目录
            trace_recorder: 追踪记录器
            checkpointer:   检查点存储器（由外部注入，支持持久化）
        """
        self.settings = settings
        self.workspace_root = workspace_root
        self.trace_recorder = trace_recorder
        self.checkpointer = checkpointer

    def _backend_for_workspace(self, workspace_path: Path) -> FilesystemBackend:
        """为指定工作区创建文件系统后端。"""
        workspace_path.mkdir(parents=True, exist_ok=True)
        return FilesystemBackend(root_dir=workspace_path, virtual_mode=True)

    def _middleware_for_workspace(self, workspace_path: Path, trace_id: str | None, agent_name: str) -> list[AgentMiddleware]:
        """为指定工作区组装中间件列表。"""
        middleware: list[AgentMiddleware] = [FilesystemPathGuardMiddleware(workspace_path)]
        if trace_id:
            middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, agent_name))
        return middleware

    def _agent_for_workspace(self, workspace_path: Path, trace_id: str | None = None):
        """为指定工作区构建完整的代理。"""
        model = build_writer_model(self.settings)
        middleware = self._middleware_for_workspace(workspace_path, trace_id, "character-service")
        return create_deep_agent(
            model=model,
            tools=[],
            system_prompt=_CHARACTER_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            backend=self._backend_for_workspace(workspace_path),
            checkpointer=self.checkpointer,
            middleware=middleware,
        )

    def delete_thread_checkpoint(self, thread_id: str) -> None:
        """删除指定线程的检查点数据。"""
        self.checkpointer.delete_thread(thread_id)

    def generate(
        self,
        payload: CharacterGenerateRequest,
        thread: ThreadSummary,
    ) -> CharacterGenerateResponse:
        """执行角色生成。"""
        if self.settings.writer_agent_mode.lower() == "mock":
            return self._mock_response(payload, thread)

        trace = self.trace_recorder.create_run(thread, "character.generate")
        try:
            prompt = self._build_user_prompt(payload, thread)
            agent = self._agent_for_workspace(Path(thread.workspace_path), trace.trace_id)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={
                    "configurable": {"thread_id": thread.thread_id},
                    "callbacks": [TraceCallbackHandler(self.trace_recorder, trace.trace_id)],
                    "recursion_limit": 200,
                },
            )
            content = self._extract_text(result)
            response = self._response_from_workspace_character(payload.fallback_name(), content, thread)
            self.trace_recorder.complete_run(thread, trace.trace_id)
            return response
        except BaseException as exc:
            self.trace_recorder.fail_run(thread, trace.trace_id, exc)
            raise

    def _build_user_prompt(self, payload: CharacterGenerateRequest, thread: ThreadSummary) -> str:
        """构建角色生成的用户提示词。"""
        context_lines = [
            f"{key}: {value}"
            for key, value in payload.loose_context().items()
            if value not in ("", [], {})
        ]
        free_text = payload.primary_text()
        context = "\n".join(context_lines) or "用户没有提供结构化字段。"
        request_text = free_text or "请根据已有工作目录内容继续优化角色。"

        name = payload.fallback_name()
        return (
            "请根据用户需求塑造一个立体、鲜活的角色形象。\n"
            "当前工作目录：/\n"
            f"当前 session：{thread.thread_id}\n"
            f"角色档案必须写入 character/{name}.md，一个人物一个文档。\n\n"
            "用户需求：\n"
            f"{request_text}\n\n"
            "可用上下文（字段可能不完整，也可能包含额外信息）：\n"
            f"{context}\n\n"
            "回复请使用自然语言纯文本，不要返回 JSON。"
        )

    def _mock_response(
        self,
        payload: CharacterGenerateRequest,
        thread: ThreadSummary,
    ) -> CharacterGenerateResponse:
        """生成模拟响应（用于测试或 mock 模式）。"""
        name = payload.fallback_name()
        desc = payload.primary_text() or "一个等待被赋予灵魂的角色"
        role = payload.role or "主角"

        response = CharacterGenerateResponse(
            mode="mock",
            thread_id=thread.thread_id,
            workspace_id=thread.workspace_id,
            session_name=thread.session_name,
            workspace_path=thread.workspace_path,
            name=name,
            identity=f"{role} — {desc}",
            appearance=(
                "中等偏瘦的体型，肩膀微塌。穿着低调但有质感，偏好深色系。"
                "左眉骨有一道浅疤，笑的时候右嘴角比左边高一点。"
            ),
            personality=(
                "冷静、克制、观察力强。"
                "看似冷漠，但对弱者有不加掩饰的温柔。"
                "核心欲望：找到一个即使知道自己所有缺点也不会离开的人。"
            ),
            current_state=(
                "所在地点：城市旧城区的独立书店二楼。"
                "人物状态：略有疲惫，精神紧绷。"
                "近期事件：刚从一场意外中脱身，尚未完全恢复。"
            ),
            relationships=(
                "与导师：敬重但保持距离，害怕让对方失望所以很少主动求助。\n"
                "与对手：表面针锋相对，实则暗暗佩服对方身上自己有但不敢拥有的勇气。\n"
                "与挚友：唯一见过{name}崩溃的人。"
            ),
        )
        response.markdown = self._format_character_markdown(response)
        return response

    def _extract_text(self, result: object) -> str:
        """从代理输出中提取文本内容。"""
        if isinstance(result, dict):
            messages = result.get("messages", [])
            for message in reversed(messages):
                content = self._message_content(message)
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text_chunks = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_chunks.append(item.get("text", ""))
                    if text_chunks:
                        return "\n".join(chunk for chunk in text_chunks if chunk)
        return str(result)

    def _message_content(self, message: object) -> object:
        """从消息对象中提取内容字段。兼容字典和消息对象。"""
        if isinstance(message, dict):
            return message.get("content")
        return getattr(message, "content", None)

    def _response_from_workspace_character(
        self,
        name: str,
        content: str,
        thread: ThreadSummary,
    ) -> CharacterGenerateResponse:
        """从工作区文件构建角色生成响应。"""
        character_path = Path(thread.workspace_path) / "character" / f"{name}.md"
        if not character_path.exists():
            raise FileNotFoundError(f"Agent did not write character/{name}.md: {character_path}")

        markdown = character_path.read_text(encoding="utf-8").strip()
        if not markdown:
            raise ValueError(f"Agent wrote an empty character/{name}.md: {character_path}")

        return CharacterGenerateResponse(
            mode="live",
            thread_id=thread.thread_id,
            workspace_id=thread.workspace_id,
            session_name=thread.session_name,
            workspace_path=thread.workspace_path,
            name=name,
            content=content,
            markdown=markdown,
        )

    def _format_character_markdown(self, response: CharacterGenerateResponse) -> str:
        """将角色响应格式化为 Markdown 文本。"""
        return (
            f"# {response.name}\n\n"
            f"## 角色身份\n\n{response.identity}\n\n"
            f"## 外貌特征\n\n{response.appearance}\n\n"
            f"## 性格与内心\n\n{response.personality}\n\n"
            f"## 关系网络\n\n{response.relationships}\n\n"
            f"## 目前状态\n\n{response.current_state}\n"
        )
