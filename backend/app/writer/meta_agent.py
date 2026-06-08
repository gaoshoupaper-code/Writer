from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress

from pathlib import Path
from typing import AsyncIterator

from deepagents import CompiledSubAgent, SubAgent, create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.writer.subagents.character_subagent import build_character_subagent
from app.writer.subagents.detail_outline_subagent import build_detail_outline_pipeline_subagent
from app.writer.models import build_writer_model
from app.writer.subagents.outline_subagent import build_outline_pipeline_subagent
from app.writer.subagents.writing_subagent import build_writing_pipeline_subagent
from app.writer.middleware import (
    ArtifactPrerequisite,
    ArtifactPrerequisiteMiddleware,
    ErrorRecoveryMiddleware,
    FilesystemPathGuardMiddleware,
    GoalMiddleware,
    TraceCallbackHandler,
    TraceMiddleware,
)
from app.core.settings import Settings
from app.create_type.store import CreateTypeStore
from app.writer.trace import TraceRecorder
from app.schemas.screenplay import (
    ScreenplayGenerateRequest,
    ScreenplayGenerateResponse,
    ThreadSummary,
)
from app.schemas.checkpoint import CheckpointMessage, CheckpointState, CheckpointToolCall

PROMPT_PATH = Path(__file__).resolve().parent / "prompt" / "meta_agent_system_prompt.md"


class MetaAgentService:
    def __init__(self, settings: Settings, workspace_root: Path, trace_recorder: TraceRecorder, style_store: CreateTypeStore, checkpointer: BaseCheckpointSaver) -> None:
        self.settings = settings
        self.workspace_root = workspace_root
        self.trace_recorder = trace_recorder
        self.style_store = style_store
        self.checkpointer = checkpointer

    def _backend_for_workspace(self, workspace_path: Path) -> FilesystemBackend:
        workspace_path.mkdir(parents=True, exist_ok=True)
        return FilesystemBackend(root_dir=workspace_path, virtual_mode=True)

    def _middleware_for_workspace(self, workspace_path: Path, trace_id: str | None, agent_name: str) -> list[AgentMiddleware]:
        middleware: list[AgentMiddleware] = [
            GoalMiddleware(),
            ErrorRecoveryMiddleware(),
            FilesystemPathGuardMiddleware(workspace_path),
        ]
        if trace_id:
            middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, agent_name))
        return middleware

    def _middleware_for_pipeline_subagent(self, workspace_path: Path, trace_id: str | None, agent_name: str) -> list[AgentMiddleware]:
        middleware: list[AgentMiddleware] = []
        prerequisites = self._artifact_prerequisites_for_pipeline_subagent(workspace_path, agent_name)
        if prerequisites:
            middleware.append(ArtifactPrerequisiteMiddleware(prerequisites))
        middleware.extend(self._middleware_for_workspace(workspace_path, trace_id, agent_name))
        return middleware

    def _artifact_prerequisites_for_pipeline_subagent(
        self,
        workspace_path: Path,
        agent_name: str,
    ) -> list[ArtifactPrerequisite]:
        if agent_name == "outline-subagent":
            return [ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True)]
        if agent_name == "detail-outline-subagent":
            return [
                ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True),
                ArtifactPrerequisite("plot outline", workspace_path / "outline.md"),
            ]
        if agent_name == "writing-subagent":
            return [
                ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True),
                ArtifactPrerequisite("plot outline", workspace_path / "outline.md"),
                ArtifactPrerequisite("detail outline", workspace_path / "detail", markdown_directory=True),
            ]
        return []

    def _resolve_style_for_subagent(self, workspace_id: str, style_key: str) -> str | None:
        """从激活风格中提取指定子代理对应的风格文本（SUFFIX）。

        Args:
            workspace_id: 工作区 ID
            style_key:    风格字段名（character_style / outline_style / detail_outline_style / writing_style）

        Returns:
            风格 SUFFIX 文本，无激活风格或该字段为空时返回 None
        """
        style_id = self.style_store.get_active_style_id(workspace_id)
        if not style_id:
            return None
        style = self.style_store.get_style(style_id)
        if not style:
            return None
        text = style.get(style_key, "")
        return text.strip() if text else None

    def _resolve_meta_style(self, workspace_id: str) -> str | None:
        style_id = self.style_store.get_active_style_id(workspace_id)
        if not style_id:
            return None
        style = self.style_store.get_style(style_id)
        if not style:
            return None
        return style.get("meta_style") or None

    def _character_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None) -> SubAgent:
        middleware = self._middleware_for_workspace(workspace_path, trace_id, "character-subagent")
        return build_character_subagent(workspace_path, middleware, style_suffix=style_suffix)

    def _outline_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None) -> CompiledSubAgent:
        return build_outline_pipeline_subagent(
            workspace_path,
            build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
            style_suffix=style_suffix,
            context_file_paths=["outline.md", "character/*.md"],
            checkpointer=self.checkpointer,
        )

    def _detail_outline_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None) -> CompiledSubAgent:
        return build_detail_outline_pipeline_subagent(
            workspace_path,
            build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
            style_suffix=style_suffix,
            context_file_paths=["outline.md", "character/*.md", "detail/overview.md", "detail/chapter-*.md"],
            checkpointer=self.checkpointer,
        )

    def _writing_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None) -> CompiledSubAgent:
        return build_writing_pipeline_subagent(
            workspace_path,
            build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
            style_suffix=style_suffix,
            context_file_paths=["outline.md", "character/*.md", "detail/*.md"],
            checkpointer=self.checkpointer,
        )

    def _general_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None) -> SubAgent:
        spec = SubAgent(**GENERAL_PURPOSE_SUBAGENT)
        spec["middleware"] = self._middleware_for_workspace(workspace_path, trace_id, "general-purpose-subagent")
        return spec

    def _agent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, workspace_id: str | None = None):
        model = build_writer_model(self.settings)
        middleware: list[AgentMiddleware] = [
            GoalMiddleware(),
            ErrorRecoveryMiddleware(),
            FilesystemPathGuardMiddleware(workspace_path, allowed_write_paths=("/demand.md",)),
        ]
        if trace_id:
            middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, "meta-agent"))
        meta_style = self._resolve_meta_style(workspace_id) if workspace_id else None
        # 每个子代理只注入对应的风格到 SUFFIX 槽位
        character_style = self._resolve_style_for_subagent(workspace_id, "character_style") if workspace_id else None
        outline_style = self._resolve_style_for_subagent(workspace_id, "outline_style") if workspace_id else None
        detail_outline_style = self._resolve_style_for_subagent(workspace_id, "detail_outline_style") if workspace_id else None
        writing_style = self._resolve_style_for_subagent(workspace_id, "writing_style") if workspace_id else None
        return create_deep_agent(
            model=model,
            tools=[],
            system_prompt=self._load_system_prompt(meta_style),
            subagents=[
                self._general_subagent_for_workspace(workspace_path, trace_id),
                self._character_subagent_for_workspace(workspace_path, trace_id, character_style),
                self._outline_subagent_for_workspace(workspace_path, trace_id, outline_style),
                self._detail_outline_subagent_for_workspace(workspace_path, trace_id, detail_outline_style),
                self._writing_subagent_for_workspace(workspace_path, trace_id, writing_style),
            ],
            backend=self._backend_for_workspace(workspace_path),
            checkpointer=self.checkpointer,
            middleware=middleware,
        )

    def _load_system_prompt(self, meta_style: str | None = None) -> str:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        if meta_style:
            prompt = f"{prompt}\n\n---\n【主控风格】\n{meta_style}\n---"
        return prompt

    def delete_thread_checkpoint(self, thread_id: str) -> None:
        self.checkpointer.delete_thread(thread_id)

    async def get_thread_checkpoint(self, thread_id: str) -> CheckpointState:
        """读取 thread 的最新 checkpoint，规范化为 CheckpointState。"""
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint = await self.checkpointer.aget(config)
        if checkpoint is None:
            print(f"[checkpoint] thread_id={thread_id} → aget returned None (no checkpoint saved)")
            return CheckpointState(thread_id=thread_id, messages=[])
        channel_values = checkpoint.get("channel_values", {})
        raw_messages = channel_values.get("messages", [])
        print(f"[checkpoint] thread_id={thread_id} → channel_keys={list(channel_values.keys())}, messages_count={len(raw_messages)}")
        messages = []
        for msg in raw_messages:
            try:
                messages.append(_normalize_message(msg))
            except Exception as exc:
                print(f"[checkpoint] skip message: {exc}")
        return CheckpointState(thread_id=thread_id, messages=messages)

    def generate(
        self,
        payload: ScreenplayGenerateRequest,
        thread: ThreadSummary,
    ) -> ScreenplayGenerateResponse:
        if self.settings.writer_agent_mode.lower() == "mock":
            return self._mock_response(payload, thread)

        trace = self.trace_recorder.create_run(thread, "screenplay.generate")
        try:
            prompt = self._build_user_prompt(payload, thread)
            agent = self._agent_for_workspace(Path(thread.workspace_path), trace.trace_id, thread.workspace_id)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={
                    "configurable": {"thread_id": thread.thread_id},
                    "callbacks": [TraceCallbackHandler(self.trace_recorder, trace.trace_id)],
                    "recursion_limit": 200,
                },
            )
            content = self._extract_text(result)
            response = self._response_from_workspace_artifacts(payload, content, thread)
            self.trace_recorder.complete_run(thread, trace.trace_id)
            return response
        except BaseException as exc:
            self.trace_recorder.fail_run(thread, trace.trace_id, exc)
            raise

    async def generate_stream(self, payload: ScreenplayGenerateRequest, thread: ThreadSummary) -> AsyncIterator[str]:
        """Stream agent execution events as SSE to the frontend."""
        if self.settings.writer_agent_mode.lower() == "mock":
            response = self._mock_response(payload, thread)
            yield _sse("final", response.model_dump())
            return

        trace = self.trace_recorder.create_run(thread, "screenplay.generate.stream")
        yield _sse("status", {"status": "started", "trace_id": trace.trace_id})
        trace_queue = self.trace_recorder.get_active_queue(trace.trace_id)
        if trace_queue is None:
            raise RuntimeError(f"Trace queue was not created: {trace.trace_id}")
        for trace_update in self._trace_updates(trace_queue):
            yield trace_update

        prompt = self._build_user_prompt(payload, thread)
        agent = self._agent_for_workspace(Path(thread.workspace_path), trace.trace_id, thread.workspace_id)

        input_messages: list[dict] = [{"role": "user", "content": prompt}]
        config = {
            "configurable": {"thread_id": thread.thread_id},
            "callbacks": [TraceCallbackHandler(self.trace_recorder, trace.trace_id)],
            "recursion_limit": 200,
        }
        active_tasks: dict[str, dict] = {}

        trace_pump = asyncio.create_task(_next_trace_update(trace_queue))
        agent_events = agent.astream_events(
            {"messages": input_messages},
            config=config,
            version="v2",
        )
        agent_task = asyncio.create_task(agent_events.__anext__())

        full_result = None
        try:
            while True:
                done, _ = await asyncio.wait(
                    {agent_task, trace_pump},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if trace_pump in done:
                    trace_update = trace_pump.result()
                    if trace_update:
                        yield trace_update
                    trace_pump = asyncio.create_task(_next_trace_update(trace_queue))

                if agent_task in done:
                    try:
                        event = agent_task.result()
                    except StopAsyncIteration:
                        break
                    agent_task = asyncio.create_task(agent_events.__anext__())

                    kind = event["event"]
                    name = event.get("name", "")
                    data = event.get("data", {})

                    if kind == "on_chat_model_end":
                        output = data.get("output")
                        tool_calls = _extract_tool_calls(output)
                        text = _extract_model_text(output)
                        evt_data = {"text": text}
                        if tool_calls:
                            evt_data["tool_calls"] = tool_calls
                        yield _sse("model_output", evt_data)

                    elif kind == "on_chain_start" and name == "tools":
                        tool_inputs = data.get("input", [])
                        parent_task_id, subagent_name = _current_parent_task(active_tasks)
                        for tc in tool_inputs:
                            if isinstance(tc, dict):
                                tool_name = tc.get("name", "unknown")
                                call_id = tc.get("id", "")
                                if tool_name == "task":
                                    sub = _extract_subagent_name(tc.get("args", {}))
                                    if call_id:
                                        active_tasks[call_id] = {"name": sub}
                                yield _sse(
                                    "tool_call",
                                    {
                                        "tool": tool_name,
                                        "input": tc.get("args", {}),
                                        "call_id": call_id,
                                        "parent_task_id": parent_task_id,
                                        "subagent_name": subagent_name,
                                    },
                                )

                    elif kind == "on_tool_end":
                        if name == "task":
                            call_id = _extract_tool_call_id(data)
                            if call_id and call_id in active_tasks:
                                del active_tasks[call_id]
                        yield _sse(
                            "tool_output",
                            {
                                "tool": name,
                                "output": str(data.get("output", ""))[:2000],
                                "call_id": _extract_tool_call_id(data),
                            },
                        )

                    elif kind == "on_chat_model_stream":
                        chunk = data.get("chunk")
                        if hasattr(chunk, "content"):
                            content = chunk.content
                        elif isinstance(chunk, dict):
                            content = chunk.get("content", "")
                        else:
                            content = ""
                        if content:
                            yield _sse("model_stream", {"content": content})

                    if kind == "on_chain_end" and name == "LangGraph":
                        full_result = data.get("output")

                for trace_update in self._trace_updates(trace_queue):
                    yield trace_update

            if full_result is None:
                raise ValueError("Agent stream ended without a LangGraph final output.")
            content = self._extract_text(full_result)
            response = self._response_from_workspace_artifacts(payload, content, thread)
            self.trace_recorder.complete_run(thread, trace.trace_id)
            for trace_update in self._trace_updates(trace_queue):
                yield trace_update
            snapshot = self.trace_recorder.read_run_snapshot(thread, trace.trace_id)
            if snapshot is None:
                raise RuntimeError(f"Trace snapshot was not found: {trace.trace_id}")
            yield _sse("trace_snapshot", snapshot.model_dump(mode="json"))
            yield _sse("final", response.model_dump())
        except asyncio.CancelledError:
            self.trace_recorder.cancel_run(thread, trace.trace_id)
            raise
        except BaseException as exc:
            self.trace_recorder.fail_run(thread, trace.trace_id, exc)
            for trace_update in self._trace_updates(trace_queue):
                yield trace_update
            raise
        finally:
            trace_pump.cancel()
            agent_task.cancel()
            with suppress(BaseException):
                await agent_events.aclose()

    def _trace_updates(self, trace_queue) -> list[str]:
        trace_events = _drain_trace_queue(trace_queue)
        if not trace_events:
            return []
        return [_sse("trace_event", trace_event) for trace_event in trace_events]

    def _build_user_prompt(self, payload: ScreenplayGenerateRequest, thread: ThreadSummary) -> str:
        context_lines = [
            f"{key}: {value}"
            for key, value in payload.loose_context().items()
            if value not in ("", [], {})
        ]
        free_text = payload.primary_text()
        context = "\n".join(context_lines) or "用户没有提供结构化字段。"
        request_text = free_text or "请根据已有工作目录内容继续优化大纲。"

        return (
            "请根据用户需求执行创作任务，根据需求规模自行判断创作范围——可能是角色、大纲、短篇片段、样章，也可能是一篇完整的长篇小说。\n"
            "当前工作目录：/\n"
            f"当前 session：{thread.thread_id}\n\n"
            "## 产物要求\n\n"
            "所有情况都必须写入 outline.md 和 evaluation.md。\n\n"
            "如果判定用户需要完整小说（长篇/中篇），还需满足以下额外产物要求：\n"
            "- 按 chapter-XX.md 格式写入 chapter/ 目录，每章一个文件。\n"
            "- 写入 state_log.md，记录全局参数、场景注册表、人物状态变化、大纲变更和完成度检查。\n"
            "- 写入 review/ 目录，每章一个审查文件。\n"
            "- 生成或更新 character/ 下的人物档案，一个人物一个文件。\n\n"
            "## 长篇写作流程\n\n"
            "如果判定为完整小说创作，遵循以下流程：\n"
            "- 第一版目标长度限制为 2万-3万字；不要扩写到 5万字以上。\n"
            "- 细纲生成采用分章推进：先调用 detail-outline 子代理生成 overview.md（章节规划总览），获取总章节数；再按章节顺序逐章调用生成各章细纲。每次只委托一个文件。\n"
            "- 正文写作采用分章推进；每次调用 writing 子代理只写一个章节，目标约 1000 字，允许 800-1500 字浮动。\n"
            "- 调用 writing 子代理时，必须提供总章节数、当前章节编号、剧情大纲、本章目标、出场人物、必须发生的 beat、承接关系、必须保留的事实和禁止改变的内容；不要提供前五章正文，writing 会自行读取 chapter/ 和 detail/。\n"
            "- 每完成一章或关键场景后，立即更新 chapter/ 对应章节文件与 state_log.md，不要等全书完成后才统一写入。\n"
            "- 每次 writing 子代理完成一个章节后，其内部会立即调用 review 子代理审查该章节的逻辑自洽性和表达清晰度，并写入 review/ 下对应章节审查文件。\n"
            "- writing 子代理内部会根据 review 结论自动修订同一章节，最多 3 轮；若仍未通过，会接受当前最好版本并在返回摘要中标记质量风险，由你决定是否调整 outline/state_log 或后续补救。\n"
            "- 允许动态调整大纲：小改自动接受；中改和大改由你作为 Director 决策，并写入 state_log.md。\n"
            "- 每个场景最多修订 3 轮；超过后必须由你决定接受、重写、调整大纲或丢弃重来。\n"
            "- 最终必须调用 evaluation 子代理评估完整小说；如果最后一次 outline 调用已经自动生成了最新 evaluation.md，可以直接基于该评估收尾。\n"
            "- 最终回复只给摘要，不要在回复中输出章节正文全文。\n\n"
            "用户需求：\n"
            f"{request_text}\n\n"
            "可用上下文（字段可能不完整，也可能包含额外信息）：\n"
            f"{context}\n\n"
            "回复请使用自然语言纯文本，不要返回 JSON。"
        )

    def _mock_response(
        self,
        payload: ScreenplayGenerateRequest,
        thread: ThreadSummary,
    ) -> ScreenplayGenerateResponse:
        title = payload.fallback_title()
        premise = payload.primary_text() or "一个需要继续深化的故事创意"
        genre = payload.genre or "剧情"
        tone = payload.tone or "电影感"
        audience = payload.audience or "大众"
        beats = [
            f"开端：建立一个带有{tone}气质的{genre}世界，核心问题来自{premise}。",
            "推动：主角被迫投入一个会改变生活秩序的目标，并第一次付出真实代价。",
            "转折：看似有效的解决方案暴露出更深层的人物欲望和关系裂缝。",
            "低谷：主角失去最依赖的选择，只能面对自己一直回避的真相。",
            "高潮/结局：外部冲突被解决，主角也用行动证明故事真正的主题。",
        ]
        response = ScreenplayGenerateResponse(
            mode="mock",
            thread_id=thread.thread_id,
            workspace_id=thread.workspace_id,
            session_name=thread.session_name,
            workspace_path=thread.workspace_path,
            title=title,
            content="\n".join(beats),
            logline=f"在这个{genre}故事中，主角必须直面{premise!r}，否则将失去完成自我转变的机会。",
            synopsis=(
                f"《{title}》是一部面向{audience}的{tone}{genre}作品。故事从一个打破日常秩序的需求开始，"
                "逐步推进到更复杂的人物选择，并在结尾让主角用关键行动回应自己的核心困境。"
            ),
            beats=beats,
        )
        response.markdown = self._format_outline_markdown(response)
        response.evaluation_markdown = self._format_mock_evaluation_markdown(response)
        return response

    def _extract_text(self, result: object) -> str:
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
        if isinstance(message, dict):
            return message.get("content")
        return getattr(message, "content", None)

    def _response_from_workspace_artifacts(
        self,
        payload: ScreenplayGenerateRequest,
        content: str,
        thread: ThreadSummary,
    ) -> ScreenplayGenerateResponse:
        title = payload.fallback_title()
        outline_path = Path(thread.workspace_path) / "outline.md"
        if not outline_path.exists():
            raise FileNotFoundError(f"Agent did not write outline.md: {outline_path}")

        markdown = outline_path.read_text(encoding="utf-8").strip()
        if not markdown:
            raise ValueError(f"Agent wrote an empty outline.md: {outline_path}")

        evaluation_path = Path(thread.workspace_path) / "evaluation.md"
        if not evaluation_path.exists():
            raise FileNotFoundError(f"Agent did not write evaluation.md: {evaluation_path}")

        evaluation_markdown = evaluation_path.read_text(encoding="utf-8").strip()
        if not evaluation_markdown:
            raise ValueError(f"Agent wrote an empty evaluation.md: {evaluation_path}")

        return ScreenplayGenerateResponse(
            mode="live",
            thread_id=thread.thread_id,
            workspace_id=thread.workspace_id,
            session_name=thread.session_name,
            workspace_path=thread.workspace_path,
            title=title,
            content=content,
            markdown=markdown,
            evaluation_markdown=evaluation_markdown,
        )

    def _format_outline_markdown(self, response: ScreenplayGenerateResponse) -> str:
        beat_lines = "\n".join(
            f"{index}. {beat}" for index, beat in enumerate(response.beats, start=1)
        )
        return (
            f"# {response.title}\n\n"
            f"## 故事核心\n\n"
            f"一句话故事：{response.logline}\n\n"
            f"故事前提：待补充\n\n"
            f"核心主题：待补充\n\n"
            f"类型与基调：待补充\n\n"
            f"## 结构骨架\n\n"
            f"结构类型：三幕式\n\n"
            f"关键转折点：\n{beat_lines}\n\n"
            f"高潮设计：待补充\n\n"
            f"## 主线：{response.title}\n\n"
            f"### 第一段：开端\n\n"
            f"发生了什么：{response.beats[0] if response.beats else '待补充'}\n"
            f"走向：\n如何衔接：\n\n"
            f"### 第二段：推动\n\n"
            f"发生了什么：{response.beats[1] if len(response.beats) > 1 else '待补充'}\n"
            f"走向：\n如何衔接：\n\n"
            f"### 第三段：转折\n\n"
            f"发生了什么：{response.beats[2] if len(response.beats) > 2 else '待补充'}\n"
            f"走向：\n如何衔接：\n\n"
            f"### 第四段：低谷\n\n"
            f"发生了什么：{response.beats[3] if len(response.beats) > 3 else '待补充'}\n"
            f"走向：\n如何衔接：\n\n"
            f"### 第五段：高潮与结局\n\n"
            f"发生了什么：{response.beats[4] if len(response.beats) > 4 else '待补充'}\n"
            f"走向：\n如何衔接：\n"
        )

    def _format_mock_evaluation_markdown(self, response: ScreenplayGenerateResponse) -> str:
        return (
            "# 大纲评估报告\n\n"
            "## 总体结论\n\n"
            "Mock 模式下的大纲具备基础五段式结构，主角目标、代价和转变方向清晰。"
            "但具体角色动机、关系压力和关键场景仍偏概括，进入正式创作前建议补充更细的角色选择与情节铺垫。\n\n"
            "## 评分\n\n"
            "- 总分：76/100\n"
            "- 修改建议：建议修改\n\n"
            "## 核心问题\n\n"
            "1. 问题：角色行动的具体触发点还不够明确。\n"
            "   - 影响：后续分场时可能出现角色被剧情推着走的问题。\n"
            "   - 证据：关键节点以功能性概述为主，缺少具体选择和代价。\n\n"
            "## 修改建议\n\n"
            "优先补充主角在推动、转折和低谷处的具体选择，让每个剧情节点由人物欲望和关系压力自然引出。\n\n"
            "## 给 outline 子代理的修订指令\n\n"
            "围绕主角的核心恐惧与欲望，补充每个关键节点中的具体行动、阻力、代价和人物关系变化。\n"
        )


def _sse(event_type: str, payload: object) -> str:
    """Format a single Server-Sent Event line."""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def _normalize_message(msg: object) -> CheckpointMessage:
    """将 LangChain BaseMessage 转为 CheckpointMessage schema。"""
    # dict 形式（从 checkpoint serde 还原）
    if isinstance(msg, dict):
        role = str(msg.get("type", msg.get("role", ""))).lower()
        content = msg.get("content", "")
        if isinstance(content, list):
            # multimodal content blocks → 拼接文本
            content = "\n".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" or isinstance(block, str)
            )
        content = str(content) if content else ""
        tool_calls = None
        raw_calls = msg.get("tool_calls")
        if isinstance(raw_calls, list):
            tool_calls = [
                CheckpointToolCall(name=str(tc.get("name", "")), id=str(tc.get("id", "")))
                for tc in raw_calls
                if isinstance(tc, dict)
            ]
        name = msg.get("name")
        return CheckpointMessage(
            role=_map_role(role),
            content=content,
            tool_calls=tool_calls,
            name=str(name) if name else None,
        )

    # LangChain message 对象
    msg_type = getattr(msg, "type", "") or ""
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" or isinstance(block, str)
        )
    content = str(content) if content else ""

    tool_calls = None
    raw_calls = getattr(msg, "tool_calls", None)
    if isinstance(raw_calls, list):
        tool_calls = [
            CheckpointToolCall(name=str(tc.get("name", "")), id=str(tc.get("id", "")))
            for tc in raw_calls
            if isinstance(tc, dict)
        ]

    name = getattr(msg, "name", None)
    return CheckpointMessage(
        role=_map_role(msg_type),
        content=content,
        tool_calls=tool_calls,
        name=str(name) if name else None,
    )


def _map_role(msg_type: str) -> str:
    """将 LangChain 消息类型映射为标准化 role。"""
    mapping = {
        "system": "system",
        "human": "human",
        "user": "human",
        "ai": "ai",
        "assistant": "ai",
        "tool": "tool",
    }
    return mapping.get(msg_type, msg_type)


async def _next_trace_update(queue) -> str:
    event = await queue.get()
    return _sse("trace_event", event.model_dump())


def _drain_trace_queue(queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait().model_dump())
    return events


def _extract_tool_calls(message: object) -> list[dict]:
    """Pull tool_call info from a chat model output message."""
    calls = []
    if hasattr(message, "tool_calls"):
        for tc in message.tool_calls:
            calls.append({"name": tc.get("name", ""), "args": tc.get("args", {})})
    elif isinstance(message, dict):
        for tc in message.get("tool_calls", []):
            calls.append({"name": tc.get("name", ""), "args": tc.get("args", {})})
    return calls


def _extract_model_text(message: object) -> str:
    """Extract text content from a chat model output message."""
    if hasattr(message, "content"):
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
    if isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
    return ""


def _extract_subagent_name(args: object) -> str:
    """Pull the subagent name from task() tool arguments."""
    if isinstance(args, dict):
        return str(args.get("subagent_type") or args.get("name") or "unknown")
    return "unknown"


def _extract_tool_call_id(data: dict) -> str | None:
    """Get the tool call ID from an on_tool_end event's data."""
    if not isinstance(data, dict):
        return None
    inp = data.get("input")
    if isinstance(inp, dict):
        cid = inp.get("id") or inp.get("call_id")
        if cid:
            return str(cid)
    return data.get("call_id")


def _current_parent_task(active_tasks: dict[str, dict]) -> tuple[str | None, str | None]:
    """Return (call_id, name) of the most recently active SubAgent task."""
    if not active_tasks:
        return None, None
    last_id = next(reversed(active_tasks))
    return last_id, active_tasks[last_id]["name"]
