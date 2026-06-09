"""Character 子代理 — 角色生成服务。

职责：
  1. 构建角色生成子代理规格（build_character_subagent）
  2. 提供角色生成的完整服务（CharacterService）

  CharacterService 是面向上层 API 的服务类，封装了：
  - 子代理构建（模型、后端、中间件、检查点）
  - 用户提示词构建
  - 代理调用和结果提取
  - 工作区文件验证
  - Mock 模式支持（用于测试）
  - 写作风格注入

  角色代理的权限配置：
  - 读取：允许读取所有文件（/**）
  - 写入：只允许写入 /character/*.md（角色档案）
  - 拒绝：禁止写入其他所有文件

使用方式：
  通过 CharacterService.generate() 方法触发角色生成。
  支持 live（真实代理调用）和 mock（模拟返回）两种模式。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deepagents import CompiledSubAgent, SubAgent, create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.writer.middleware import FilesystemPathGuardMiddleware, TraceCallbackHandler, TraceMiddleware
from app.writer.models import build_writer_model
from app.writer.subagents.deep_subagent_factory import build_deep_subagent
from app.writer.subagents.evaluation_subagent import EvaluationType, build_evaluation_subagent
from app.core.settings import Settings
from app.create_type.store import CreateTypeStore
from app.writer.trace import TraceRecorder
from app.schemas.character import (
    CharacterGenerateRequest,
    CharacterGenerateResponse,
)
from app.schemas.screenplay import ThreadSummary

# 角色子代理的系统提示词文件路径（统一存放在 writer/prompt/ 目录）
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "character_system_prompt.md"


def _apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。"""
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"


def build_character_subagent(workspace_root: Path, middleware: list[AgentMiddleware] | None = None, style_suffix: str | None = None) -> SubAgent:
    """构建角色生成子代理规格。

    Args:
        workspace_root: 工作区根目录（当前未直接使用，保留扩展）
        middleware:     额外中间件列表（可选）
        style_suffix:   角色风格 SUFFIX 文本（可选）

    Returns:
        角色子代理规格字典
    """
    system_prompt = _apply_style_suffix(PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix)
    permissions = [
        FilesystemPermission(
            operations=["read"],
            paths=["/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/character/*.md"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

    spec = SubAgent(
        name="character",
        description=(
            "适用：需要生成或修改角色档案、人物关系、动机、心理矛盾或角色弧光时调用。"
            "委托时不要只给文件路径；请说明角色任务目标、故事上下文、已有设定、关键约束和期望产物。"
        ),
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_character_deep_subagent(
    workspace_root: Path,
    model: object,
    backend: object,
    middleware_factory: Callable[[str], list[AgentMiddleware]],
    style_suffix: str | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 character 子代理（内含 evolution 评估循环）。

    替代裸的 build_character_subagent，用于 meta_agent 路径。
    子代理自主决策：生成角色 → 调用 evolution 评估 → 根据反馈修订（最多 3 轮）。

    注意：CharacterService 独立 API 端点仍使用 create_deep_agent（无 evolution），
    本函数仅用于 meta_agent 的子代理注册。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:  中间件工厂函数，按 agent_name 生成对应中间件列表
        style_suffix:        角色风格 SUFFIX 文本（可选）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 system prompt + permissions ----
    primary_middleware = list(middleware_factory("character-subagent"))
    primary_spec = build_character_subagent(workspace_root, primary_middleware, style_suffix)

    # ---- evolution 子代理规格 ----
    evaluation_spec = build_evaluation_subagent(
        EvaluationType.CHARACTER,
        workspace_root,
        middleware_factory("character-evaluation-subagent"),
        context_file_paths=["character/*.md"],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description="评估角色档案的塑造质量，写入 character/evaluation.md，返回评分和修订建议。",
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- 组装 system prompt：追加 evolution 使用指令 ----
    base_prompt = primary_spec["system_prompt"]
    if "评估机制" in base_prompt:
        base_prompt = base_prompt.split("评估机制")[0].rstrip()
    evolution_suffix = (
        "评估机制（evolution 子代理）：\n"
        "- 你有一个名为 \"evolution\" 的子代理，用于评估你的角色塑造质量。\n"
        "- 工作流程：完成 character/ 下角色档案写入后，调用 evolution 子代理评估质量。\n"
        "- evolution 会读取 character/ 下的所有角色文件并写入评估报告到 character/evaluation.md，然后返回评分和修改建议。\n"
        "- 如果 evolution 返回\"建议修改\"或\"必须修改\"，你**必须**读取 character/evaluation.md 中的详细评估报告，"
        "根据核心问题和修改建议修订角色档案，然后再次调用 evolution 评估修订后的版本。\n"
        "- 如果 evolution 返回\"无需修改\"，直接向父代理返回结果。\n"
        "- 最多调用 evolution 3 次（含首次评估），超过后系统会强制终止评估循环。\n"
        "- 返回父代理时，请在回复中包含：修订轮数、是否有质量风险。"
    )
    system_prompt = f"{base_prompt}\n\n{evolution_suffix}"

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="character",
        description=(
            "适用：需要生成或修改角色档案、人物关系、动机、心理矛盾或角色弧光时调用。"
            "内置 evolution 评估循环：生成角色后自动评估质量，如果评估建议修订会自动修订，最多 3 轮。"
            "委托时不要只给文件路径；请说明角色任务目标、故事上下文、已有设定、关键约束和期望产物。"
        ),
        model=model,
        system_prompt=system_prompt,
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        max_revisions=3,
    )


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

    def __init__(self, settings: Settings, workspace_root: Path, trace_recorder: TraceRecorder, style_store: CreateTypeStore, checkpointer: BaseCheckpointSaver) -> None:
        """
        Args:
            settings:       应用配置（包含模型、模式等设置）
            workspace_root: 工作区根目录
            trace_recorder: 追踪记录器
            style_store:    写作风格存储
            checkpointer:   检查点存储器（由外部注入，支持持久化）
        """
        self.settings = settings
        self.workspace_root = workspace_root
        self.trace_recorder = trace_recorder
        self.style_store = style_store
        self.checkpointer = checkpointer

    def _backend_for_workspace(self, workspace_path: Path) -> FilesystemBackend:
        """为指定工作区创建文件系统后端。

        Args:
            workspace_path: 工作区路径（自动创建目录）

        Returns:
            配置好的 FilesystemBackend 实例（虚拟模式）
        """
        workspace_path.mkdir(parents=True, exist_ok=True)
        return FilesystemBackend(root_dir=workspace_path, virtual_mode=True)

    def _middleware_for_workspace(self, workspace_path: Path, trace_id: str | None, agent_name: str) -> list[AgentMiddleware]:
        """为指定工作区组装中间件列表。

        中间件顺序（从内到外）：
        1. FilesystemPathGuardMiddleware — 路径安全守卫
        2. TraceMiddleware（可选）      — 执行链路追踪

        Args:
            workspace_path: 工作区路径
            trace_id:       追踪 ID（None 时不添加追踪中间件）
            agent_name:     代理名称

        Returns:
            中间件列表
        """
        middleware: list[AgentMiddleware] = [FilesystemPathGuardMiddleware(workspace_path)]
        if trace_id:
            middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, agent_name))
        return middleware

    def _agent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None):
        """为指定工作区构建完整的代理。

        组装：模型 + 系统提示词 + 后端 + 检查点 + 中间件。

        Args:
            workspace_path: 工作区路径
            trace_id:       追踪 ID（可选）
            style_suffix:   角色风格 SUFFIX 文本（可选）

        Returns:
            可调用的代理实例
        """
        model = build_writer_model(self.settings)
        middleware = self._middleware_for_workspace(workspace_path, trace_id, "character-service")
        return create_deep_agent(
            model=model,
            tools=[],
            system_prompt=self._load_system_prompt(style_suffix),
            backend=self._backend_for_workspace(workspace_path),
            checkpointer=self.checkpointer,
            middleware=middleware,
        )

    def _load_system_prompt(self, style_suffix: str | None = None) -> str:
        """加载系统提示词，可选追加写作风格 SUFFIX。"""
        return _apply_style_suffix(PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix)

    def _resolve_style_suffix(self, workspace_id: str) -> str | None:
        """从风格存储中获取当前工作区的激活角色风格 SUFFIX。

        Args:
            workspace_id: 工作区 ID

        Returns:
            角色风格文本，无激活风格或该字段为空时返回 None
        """
        style_id = self.style_store.get_active_style_id(workspace_id)
        if not style_id:
            return None
        style = self.style_store.get_style(style_id)
        if not style:
            return None
        text = style.get("character_style", "")
        return text.strip() if text else None

    def delete_thread_checkpoint(self, thread_id: str) -> None:
        """删除指定线程的检查点数据。

        用于清理已删除线程的状态数据。
        """
        self.checkpointer.delete_thread(thread_id)

    def generate(
        self,
        payload: CharacterGenerateRequest,
        thread: ThreadSummary,
    ) -> CharacterGenerateResponse:
        """执行角色生成。

        完整流程：
        1. 检查是否为 mock 模式（如果是，返回模拟数据）
        2. 创建追踪记录
        3. 构建用户提示词
        4. 解析写作风格
        5. 构建并调用代理
        6. 验证工作区产物文件
        7. 完成追踪并返回结果

        Args:
            payload: 角色生成请求（包含角色信息、上下文等）
            thread:  线程信息（包含工作区路径、线程 ID 等）

        Returns:
            角色生成响应（包含角色信息和 Markdown 内容）

        Raises:
            FileNotFoundError: 代理未写入角色文件
            ValueError:        代理写入了空的角色文件
        """
        if self.settings.writer_agent_mode.lower() == "mock":
            return self._mock_response(payload, thread)

        trace = self.trace_recorder.create_run(thread, "character.generate")
        try:
            prompt = self._build_user_prompt(payload, thread)
            style_suffix = self._resolve_style_suffix(thread.workspace_id)
            agent = self._agent_for_workspace(Path(thread.workspace_path), trace.trace_id, style_suffix)
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
        """构建角色生成的用户提示词。

        将请求中的结构化字段和自由文本组合为自然语言提示词，
        引导代理将角色档案写入 character/<name>.md。

        Args:
            payload: 角色生成请求
            thread:  线程信息

        Returns:
            完整的用户提示词
        """
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
        """生成模拟响应（用于测试或 mock 模式）。

        返回一个预设的角色数据，不调用真实代理。

        Args:
            payload: 角色生成请求
            thread:  线程信息

        Returns:
            包含预设角色数据的响应
        """
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
        """从代理输出中提取文本内容。

        从消息列表中反向搜索最后一条包含文本内容的消息。
        支持字符串内容和列表内容（多模态消息格式）。
        """
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
        """从工作区文件构建角色生成响应。

        读取代理写入的 character/<name>.md 文件，验证内容非空，
        构建包含 Markdown 内容的响应。

        Args:
            name:    角色名称
            content: 代理的文本输出
            thread:  线程信息

        Returns:
            包含工作区文件内容的角色生成响应

        Raises:
            FileNotFoundError: 角色文件不存在
            ValueError:        角色文件为空
        """
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
        """将角色响应格式化为 Markdown 文本。

        用于 mock 模式下生成角色档案的 Markdown 内容。

        Args:
            response: 角色生成响应

        Returns:
            格式化的 Markdown 文本
        """
        return (
            f"# {response.name}\n\n"
            f"## 角色身份\n\n{response.identity}\n\n"
            f"## 外貌特征\n\n{response.appearance}\n\n"
            f"## 性格与内心\n\n{response.personality}\n\n"
            f"## 关系网络\n\n{response.relationships}\n\n"
            f"## 目前状态\n\n{response.current_state}\n"
        )
