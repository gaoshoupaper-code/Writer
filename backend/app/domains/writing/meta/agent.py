from __future__ import annotations

import asyncio
import json
import re

from pathlib import Path
from typing import Any, AsyncIterator

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.platform.agent.base_service import BaseAgentService
from app.platform.agent.runtime import (
    GENERAL_PURPOSE_SUBAGENT,
    CompiledSubAgent,
    FilesystemBackend,
    SubAgent,
    compose_skills_backend,
    create_deep_agent,
)
from app.platform.streaming import ExtraTask, run_agent_stream
from app.domains.writing.expert_agent.agents.storybuilding import build_storybuilding_deep_subagent
from app.domains.writing.expert_agent.services.storyline_graph import generate_storyline_graph
from app.domains.writing.expert_agent.agents.detail_outline import build_detail_outline_deep_subagent
from app.domains.writing.models import build_writer_model
from app.domains.writing.expert_agent.agents.writing import build_writing_deep_subagent
from app.domains.writing.expert_agent.agents.interview import build_interview_deep_subagent
from app.platform.agent.middleware import (
    ArtifactPrerequisite,
    ArtifactPrerequisiteMiddleware,
    ErrorRecoveryMiddleware,
    FilesystemPathGuardMiddleware,
    FileWriteSerializeMiddleware,
    TraceCallbackHandler,
    TraceMiddleware,
)
from app.domains.writing.middleware import GoalMiddleware, MetaReadOnlyMiddleware
from app.platform.core.settings import Settings
from app.domains.writing.styling.store import CreateTypeStore
from app.platform.trace import TraceRecorder
from app.schemas.screenplay import (
    ScreenplayGenerateRequest,
    ScreenplayGenerateResponse,
    ThreadSummary,
)
from app.schemas.checkpoint import CheckpointMessage, CheckpointState, CheckpointToolCall

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
META_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# SSE 心跳间隔已收敛到 platform.streaming.DEFAULT_HEARTBEAT_INTERVAL（PR-07a）。


class MetaAgentService(BaseAgentService):
    def __init__(self, settings: Settings, workspace_root: Path, trace_recorder: TraceRecorder, style_store: CreateTypeStore, checkpointer: BaseCheckpointSaver) -> None:
        # 复用 BaseAgentService 的通用初始化（settings/workspace_root/trace_recorder/checkpointer）
        super().__init__(settings, workspace_root, trace_recorder, checkpointer)
        self.style_store = style_store

    def _middleware_for_workspace(self, workspace_path: Path, trace_id: str | None, agent_name: str) -> list[AgentMiddleware]:
        # GoalMiddleware 仅安装在 Meta Agent 层级（见 _agent_for_workspace），
        # 子代理不需要——它们通过 evolution 评估循环和 ArtifactValidation 控制质量。
        middleware: list[AgentMiddleware] = [
            ErrorRecoveryMiddleware(),
            FilesystemPathGuardMiddleware(workspace_path),
            FileWriteSerializeMiddleware(),
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
        if agent_name == "detail-outline-subagent":
            return [
                ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True),
                ArtifactPrerequisite("plot outline", workspace_path / "outline.md"),
                ArtifactPrerequisite("worldview", workspace_path / "worldview.md"),
                ArtifactPrerequisite("storylines", workspace_path / "storyline", markdown_directory=True),
            ]
        if agent_name == "writing-subagent":
            return [
                ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True),
                ArtifactPrerequisite("plot outline", workspace_path / "outline.md"),
                ArtifactPrerequisite("storylines", workspace_path / "storyline", markdown_directory=True),
                ArtifactPrerequisite("detail outline", workspace_path / "detail", markdown_directory=True),
            ]
        return []

    # ── 模型构建（PR-12：从基类下沉，消除 platform→domains 反向依赖）─────
    def _build_model_default(self):
        """写作领域的默认模型（无 owner/key 时）。"""
        return build_writer_model(self.settings)

    def _build_model_with_key(self, key: str, base_url: str | None, model_name: str | None):
        """按 owner 的 key 构建写作模型（多用户隔离 D9）。"""
        return build_writer_model(
            self.settings, api_key=key, base_url=base_url, model_name_override=model_name,
        )

    def _resolve_style_for_subagent(self, workspace_id: str, style_key: str) -> str | None:
        """从激活风格中提取指定子代理对应的风格文本（SUFFIX）。

        Args:
            workspace_id: 工作区 ID
            style_key:    风格字段名（storybuilding_style / detail_outline_style / writing_style）

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

    def _interview_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, *, model=None) -> CompiledSubAgent:
        return build_interview_deep_subagent(
            workspace_path,
            model or build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_workspace(workspace_path, trace_id, agent_name),
        )

    def _storybuilding_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None, *, model=None) -> CompiledSubAgent:
        return build_storybuilding_deep_subagent(
            workspace_path,
            model or build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_workspace(workspace_path, trace_id, agent_name),
            style_suffix=style_suffix,
            context_file_paths=["demand.md"],
        )

    def _detail_outline_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None, *, model=None) -> CompiledSubAgent:
        return build_detail_outline_deep_subagent(
            workspace_path,
            model or build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
            style_suffix=style_suffix,
            context_file_paths=["demand.md", "outline.md", "character/*.md", "worldview.md", "storyline.md", "storyline/*.md", "detail/overview.md", "detail/chapter-*.md"],
        )

    def _writing_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, style_suffix: str | None = None, *, model=None) -> CompiledSubAgent:
        return build_writing_deep_subagent(
            workspace_path,
            model or build_writer_model(self.settings),
            self._backend_for_workspace(workspace_path),
            lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
            style_suffix=style_suffix,
            context_file_paths=["demand.md", "outline.md", "character/*.md", "worldview.md", "storyline.md", "storyline/*.md", "detail/*.md"],
        )

    def _general_subagent_for_workspace(self, workspace_path: Path, trace_id: str | None = None) -> SubAgent:
        spec = SubAgent(**GENERAL_PURPOSE_SUBAGENT)
        spec["middleware"] = self._middleware_for_workspace(workspace_path, trace_id, "general-purpose-subagent")
        return spec

    def _agent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, workspace_id: str | None = None, *, model=None, checkpointer=None):
        # 多用户隔离（T2.4/T2.5）：model 用用户解密 key 构建，
        # checkpointer 用用户的分库 saver。两者外部注入，缺省回退全局（管理员兜底）。
        if model is None:
            model = build_writer_model(self.settings)
        if checkpointer is None:
            checkpointer = self.checkpointer
        middleware: list[AgentMiddleware] = [
            GoalMiddleware(),
            ErrorRecoveryMiddleware(),
            MetaReadOnlyMiddleware(),
        ]
        if trace_id:
            middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, "meta-agent"))
        meta_style = self._resolve_meta_style(workspace_id) if workspace_id else None
        # 每个子代理只注入对应的风格到 SUFFIX 槽位
        storybuilding_style = self._resolve_style_for_subagent(workspace_id, "storybuilding_style") if workspace_id else None
        detail_outline_style = self._resolve_style_for_subagent(workspace_id, "detail_outline_style") if workspace_id else None
        writing_style = self._resolve_style_for_subagent(workspace_id, "writing_style") if workspace_id else None
        backend = self._backend_for_workspace(workspace_path)
        effective_backend, skill_sources = compose_skills_backend(backend, self._meta_skill_paths())
        return create_deep_agent(
            model=model,
            tools=[],
            system_prompt=self._load_system_prompt(meta_style),
            subagents=[
                self._general_subagent_for_workspace(workspace_path, trace_id),
                self._interview_subagent_for_workspace(workspace_path, trace_id, model=model),
                self._storybuilding_subagent_for_workspace(workspace_path, trace_id, storybuilding_style, model=model),
                self._detail_outline_subagent_for_workspace(workspace_path, trace_id, detail_outline_style, model=model),
                self._writing_subagent_for_workspace(workspace_path, trace_id, writing_style, model=model),
            ],
            backend=effective_backend,
            checkpointer=checkpointer,
            middleware=middleware,
            skills=skill_sources,
        )

    def _load_system_prompt(self, meta_style: str | None = None) -> str:
        prompt = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8").strip()
        if meta_style:
            prompt = f"{prompt}\n\n---\n【主控风格】\n{meta_style}\n---"
        return prompt

    def _meta_skill_paths(self) -> list[str]:
        return [
            str(META_SKILLS_DIR / "auto-pipeline"),
            str(META_SKILLS_DIR / "interactive-gating"),
        ]

    async def get_thread_checkpoint(self, thread_id: str, *, owner_id: str | None = None) -> CheckpointState:
        """读取 thread 的最新 checkpoint，规范化为 CheckpointState。"""
        checkpointer = await self._resolve_checkpointer(owner_id)
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint = await checkpointer.aget(config)
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
        *,
        owner_id: str | None = None,
    ) -> ScreenplayGenerateResponse:
        if self.settings.writer_agent_mode.lower() == "mock":
            return self._mock_response(payload, thread)

        trace = self.trace_recorder.create_run(thread, "screenplay.generate")
        try:
            prompt = self._build_user_prompt(payload, thread)
            model = self._resolve_model(owner_id)
            agent = self._agent_for_workspace(
                Path(thread.workspace_path), trace.trace_id, thread.workspace_id,
                model=model,
            )
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
            # storybuilding 产物稳定后派生流程图（幂等：非 storybuilding 调用时 storyline.md 未变，图不变）
            generate_storyline_graph(Path(thread.workspace_path))
            self.trace_recorder.complete_run(thread, trace.trace_id)
            return response
        except BaseException as exc:
            self.trace_recorder.fail_run(thread, trace.trace_id, exc)
            raise

    async def generate_stream(self, payload: ScreenplayGenerateRequest, thread: ThreadSummary, *, owner_id: str | None = None) -> AsyncIterator[str]:
        """Stream agent execution events as SSE to the frontend.

        SSE 编排骨架（心跳 + astream_events 循环 + interrupt 检测）由
        ``platform.streaming.run_agent_stream`` 提供，事件分发逻辑通过
        闭包 sink 注入（领域专属：model_output/tool_call/task 派发/章节计数）。
        """
        if self.settings.writer_agent_mode.lower() == "mock":
            response = self._mock_response(payload, thread)
            yield _sse("final", response.model_dump())
            return

        # 多用户：解析当前用户的 model 与分库 checkpointer
        model = self._resolve_model(owner_id)
        checkpointer = await self._resolve_checkpointer(owner_id)

        # HITL resume：带 resume + trace_id 时复用活跃 trace（不发 run_start），
        # 否则 create_run 新开。内存丢失（服务重启）时 resume_run 会降级 create_run（D2=A）。
        resume_value = getattr(payload, "resume", None)
        trace_id_in = getattr(payload, "trace_id", None)
        if resume_value is not None and trace_id_in:
            trace, is_new = self.trace_recorder.resume_run(thread, trace_id_in)
        else:
            trace = self.trace_recorder.create_run(thread, "screenplay.generate.stream")
            is_new = True
        if is_new:
            yield _sse("status", {"status": "started", "trace_id": trace.trace_id})
        trace_queue = self.trace_recorder.get_active_queue(trace.trace_id)
        if trace_queue is None:
            raise RuntimeError(f"Trace queue was not created: {trace.trace_id}")
        for trace_update in self._trace_updates(trace_queue):
            yield trace_update

        agent = self._agent_for_workspace(
            Path(thread.workspace_path), trace.trace_id, thread.workspace_id,
            model=model, checkpointer=checkpointer,
        )

        # resume 分支已在上方判定，据此构造 agent 输入
        if resume_value is not None:
            agent_input: object = Command(resume=resume_value)
        else:
            prompt = self._build_user_prompt(payload, thread)
            agent_input = {"messages": [{"role": "user", "content": prompt}]}
        config = {
            "configurable": {"thread_id": thread.thread_id},
            "callbacks": [TraceCallbackHandler(self.trace_recorder, trace.trace_id)],
            "recursion_limit": 200,
        }
        active_tasks: dict[str, dict] = {}
        # 子代理调用计数：按 subagent_type 累计，用于 storybuilding 轮次与 writing 章节号推断降级（D6）
        subagent_call_counts: dict[str, int] = {}

        # ── 事件分发 sink（领域专属，闭包捕获 thread/active_tasks/计数器）─────
        async def on_event(event: dict) -> list[str]:
            frames: list[str] = []
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
                frames.append(_sse("model_output", evt_data))

            elif kind == "on_chain_start" and name == "tools":
                tool_inputs = data.get("input", [])
                parent_task_id, subagent_name = _current_parent_task(active_tasks)
                for tc in tool_inputs:
                    if isinstance(tc, dict):
                        tool_name = tc.get("name", "unknown")
                        call_id = tc.get("id", "")
                        call_payload: dict[str, Any] = {
                            "tool": tool_name,
                            "input": tc.get("args", {}),
                            "call_id": call_id,
                            "parent_task_id": parent_task_id,
                            "subagent_name": subagent_name,
                        }
                        # task 工具：派发焦点信息（D6/D7）—— 章节号、总章数、轮次
                        if tool_name == "task":
                            args = tc.get("args", {}) or {}
                            sub = _extract_subagent_name(args)
                            description = str(args.get("description", "") or "")
                            subagent_call_counts[sub] = subagent_call_counts.get(sub, 0) + 1
                            call_ordinal = subagent_call_counts[sub]
                            chapter_index = _extract_chapter_index(description)
                            total_chapters = await asyncio.to_thread(
                                _count_total_chapters, Path(thread.workspace_path)
                            )
                            # writing 章节号降级：正则失败时按 writing 调用序推断（D6）
                            if sub == "writing" and chapter_index is None:
                                chapter_index = call_ordinal
                            if call_id:
                                active_tasks[call_id] = {
                                    "name": sub,
                                    "chapter_index": chapter_index,
                                    "total_chapters": total_chapters,
                                    "iteration": call_ordinal,
                                }
                            call_payload["subagent_type"] = sub
                            call_payload["chapter_index"] = chapter_index
                            call_payload["total_chapters"] = total_chapters
                            call_payload["iteration"] = call_ordinal
                        frames.append(_sse("tool_call", call_payload))

            elif kind == "on_tool_end":
                output_payload: dict[str, Any] = {
                    "tool": name,
                    "output": str(data.get("output", ""))[:2000],
                    "call_id": _extract_tool_call_id(data),
                }
                if name == "task":
                    call_id = _extract_tool_call_id(data)
                    if call_id and call_id in active_tasks:
                        task_meta = active_tasks.pop(call_id)
                        finished_subagent = task_meta.get("name")
                        # storybuilding 完成后派生流程图：确定性、纯后端。
                        # 失败已被模块内部 try/except 吞掉，这里只读不抛，绝不阻断 SSE 流。
                        if finished_subagent == "storybuilding":
                            await asyncio.to_thread(
                                generate_storyline_graph, Path(thread.workspace_path)
                            )
                        # writing 写完章节后实时算字数（D7），随 tool_output 推给前端
                        if finished_subagent == "writing":
                            chapter_index = task_meta.get("chapter_index")
                            if chapter_index:
                                word_count = await asyncio.to_thread(
                                    _count_chapter_words,
                                    Path(thread.workspace_path),
                                    chapter_index,
                                )
                                if word_count is not None:
                                    output_payload["word_count"] = word_count
                                    output_payload["chapter_index"] = chapter_index
                frames.append(_sse("tool_output", output_payload))

            elif kind == "on_tool_error":
                # 单工具失败：推 tool_error SSE，前端据此标红/重试中（点1 S4）
                # data.error 在部分版本缺失，output 常是异常对象，兜底取
                err = data.get("error") or data.get("output") or ""
                frames.append(_sse("tool_error", {
                    "tool": name,
                    "call_id": _extract_tool_call_id(data),
                    "error": str(err)[:500],
                }))

            elif kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                if hasattr(chunk, "content"):
                    content = chunk.content
                elif isinstance(chunk, dict):
                    content = chunk.get("content", "")
                else:
                    content = ""
                if content:
                    frames.append(_sse("model_stream", {"content": content}))

            return frames

        # ── trace_pump 作为额外并发任务（与 agent 事件/心跳公平竞争 asyncio.wait）──
        def _on_trace_done(finished: asyncio.Task) -> tuple[asyncio.Task | None, list[str]]:
            update = finished.result()
            return asyncio.create_task(_next_trace_update(trace_queue)), [update] if update else []

        trace_extra = ExtraTask(
            task=asyncio.create_task(_next_trace_update(trace_queue)),
            on_done=_on_trace_done,
        )

        # 闭包 sink：领域专属事件分发（适配 EventSink 协议，on_event 是协程）
        class _WritingSink:
            async def on_event(self_inner, event: dict) -> list[str]:
                return await on_event(event)

        sse_iter, result = run_agent_stream(
            agent, agent_input, config,
            sink=_WritingSink(),
            extra_tasks=[trace_extra],
        )

        try:
            async for frame in sse_iter:
                yield frame

            # ── 流结束：trace drain + interrupt 检测 + final ──────────────────────
            for trace_update in self._trace_updates(trace_queue):
                yield trace_update

            if result.pending_interrupt is not None:
                iv = result.pending_interrupt
                interrupt_payload: dict[str, Any] = {
                    "thread_id": thread.thread_id,
                    "source": "interview",
                }
                if isinstance(iv, dict):
                    interrupt_payload["question"] = iv.get("question", "")
                    interrupt_payload["options"] = iv.get("options")
                    interrupt_payload["multi_select"] = iv.get("multi_select", False)
                yield _sse("interrupt", interrupt_payload)
                return

            if result.full_result is None:
                raise ValueError("Agent stream ended without a LangGraph final output.")
            content = self._extract_text(result.full_result)
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
            "请根据用户需求执行创作任务。\n"
            "当前工作目录：/\n"
            f"当前 session：{thread.thread_id}\n\n"

            "## 故事构建流程\n\n"
            "对于需要完整故事构建的需求（长篇/中篇），采用迭代式构建：\n"
            "1. 判断故事规模，确定需要的迭代轮数（通常 2-4 轮）。\n"
            "2. 第 1 轮委托 storybuilding，任务描述中写明「使用 skeleton skill」。\n"
            "3. 第 2+ 轮委托 storybuilding，任务描述中写明「使用 expand skill」，传入：用户扩展方向（如有）、本轮焦点。\n"
            "4. 每轮返回后检查评估结果；如标记质量风险，在下一轮优先修复。\n"
            "5. 循环结束后进入 detail-outline → writing 阶段。\n\n"

            "## storybuilding 委托规范\n\n"
            "每次委托 storybuilding 时必须明确传达：\n"
            "- 使用哪套 skill：skeleton（第 1 轮）或 expand（第 2+ 轮）\n"
            "- 本轮焦点：优先扩展哪些维度（人物/故事线/世界观/总纲/卷纲）\n"
            "- 用户的扩展方向（如有）\n"
            "- 前几轮评估中发现的问题（如有）\n\n"

            "## 后续阶段\n\n"
            "storybuilding 迭代完成后，进入细纲和正文阶段：\n"
            "- detail-outline：先调用生成 detail/overview.md（章节规划总览），获取总章节数；再按章节顺序逐章调用，每次只委托一个文件。\n"
            "- writing：每次调用只写一个章节，目标约 1000 字，允许 800-1500 字浮动。\n"
            "- 调用 writing 子代理时，必须提供总章节数、当前章节编号、剧情大纲、本章目标、出场人物、必须发生的 beat、承接关系、必须保留的事实和禁止改变的内容；不要提供前五章正文，writing 会自行读取 chapter/ 和 detail/。\n"
            "- 每完成一章后，立即更新对应章节文件，不要等全书完成后才统一写入。\n"
            "- writing 子代理内部 evolution 会自动审查章节质量（单次审查修订）；若不通过，接受当前版本并在返回摘要中标记质量风险。\n"
            "- 允许动态调整大纲：小改自动接受；中改和大改由你作为 Director 决策。\n"
            "- 最终回复只给摘要，不要在回复中输出章节正文全文。\n\n"

            "## 产物要求\n\n"
            "所有情况都必须写入 outline.md 和 evaluation.md。\n\n"
            "如果判定用户需要完整小说（长篇/中篇），还需满足以下额外产物要求：\n"
            "- 按 chapter-XX.md 格式写入 chapter/ 目录，每章一个文件。\n"
            "- 写入 state_log.md，记录全局参数、场景注册表、人物状态变化、大纲变更和完成度检查。\n"
            "- 写入 review/ 目录，每章一个审查文件。\n\n"

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


# ── 焦点信息辅助（D6/D7）：章节号正则 + 总章数 + 字数，纯后端计算，零侵入 Meta Agent ──

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int | None:
    """中文数字转整数（支持 十/百，如"二十三"→23）。无法解析返回 None。"""
    if not text:
        return None
    total = 0
    section = 0
    for ch in text:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            section = section if section else 1
            total += section * 10
            section = 0
        elif ch == "百":
            section = section if section else 1
            total += section * 100
            section = 0
        else:
            return None
    return total + section if (total or section) else None


def _cn_or_int(token: str) -> int | None:
    """将 "3" 或 "二十三" 统一转为整数。"""
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _cn_to_int(token)


def _extract_chapter_index(description: str) -> int | None:
    """从 task 描述中正则提取章节号（D6）。支持 "第3章" / "第三章" / "chapter 3" 等。"""
    if not description:
        return None
    # "第3章" / "第三章" / "第 3 章"
    m = re.search(r"第\s*([0-9一二三四五六七八九十百]+)\s*章", description)
    if m:
        return _cn_or_int(m.group(1))
    # "chapter 3" / "chapter-03" / "chapter_3"
    m = re.search(r"chapter[\s\-_]*([0-9]+)", description, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _count_total_chapters(workspace_path: Path) -> int | None:
    """总章数 = detail/chapter-*.md 的最大编号（D6）。

    detail 阶段完成后才进入 writing，此时细纲齐全；取最大编号比正则 overview 更鲁棒。
    """
    detail_dir = workspace_path / "detail"
    if not detail_dir.exists():
        return None
    nums: list[int] = []
    for path in detail_dir.glob("chapter-*.md"):
        m = re.search(r"chapter-(\d+)", path.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def _count_chapter_words(workspace_path: Path, chapter_index: int) -> int | None:
    """读 chapter/chapter-XX.md 算去空白字符数（D7 焦点文案字数）。"""
    chapter_dir = workspace_path / "chapter"
    for name in (f"chapter-{chapter_index:02d}.md", f"chapter-{chapter_index}.md"):
        path = chapter_dir / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return None
            return len(re.sub(r"\s", "", text))
    return None
