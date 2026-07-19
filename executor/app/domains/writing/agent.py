from __future__ import annotations

import asyncio
import json
import logging

from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.platform.agent.base_service import BaseAgentService
from app.platform.streaming import ExtraTask, run_agent_stream
from app.domains.writing.events import WritingEventSink
from app.domains.writing.expert_agent.services.storyline_graph import generate_storyline_graph
from app.domains.writing.models import build_writer_model
from app.platform.agent.middleware import (
    TraceCallbackHandler,
    TraceMiddleware,
)
from app.platform.agent.loader import load_current_package
from app.platform.memory import get_memory_backend
from app.platform.core.settings import Settings
from app.domains.writing.styling.store import CreateTypeStore
from app.platform.trace import TraceRecorder
from app.schemas.screenplay import (
    ScreenplayGenerateRequest,
    ScreenplayGenerateResponse,
    ThreadSummary,
)

# SSE 心跳间隔已收敛到 platform.streaming.DEFAULT_HEARTBEAT_INTERVAL（PR-07a）。

logger = logging.getLogger("writer.meta_agent")


def _get_memory_recall_cls():
    """从 harness 包动态加载 MemoryRecallMiddleware 类。

    MemoryRecallMiddleware 定义在 harness 包的 middleware/ 下（可进化要素），
    executor 运行时不硬依赖它——通过 load_current_package 动态获取。
    harness 包尚未实现该 middleware 时返回 None（向后兼容，走全量注入）。
    """
    try:
        import importlib

        pkg = load_current_package()
        mw_module = importlib.import_module(f"{pkg.__name__}.middleware.memory_recall_middleware")
        return getattr(mw_module, "MemoryRecallMiddleware", None)
    except Exception as e:
        logger.debug("MemoryRecallMiddleware 加载失败（走全量注入）：%s", e)
        return None


class MetaAgentService(BaseAgentService):
    def __init__(self, settings: Settings, workspace_root: Path, trace_recorder: TraceRecorder, style_store: CreateTypeStore, checkpointer: BaseCheckpointSaver) -> None:
        # 复用 BaseAgentService 的通用初始化（settings/workspace_root/trace_recorder/checkpointer）
        super().__init__(settings, workspace_root, trace_recorder, checkpointer)
        self.style_store = style_store

    # ── 模型构建（PR-12：从基类下沉，消除 platform→domains 反向依赖）─────
    def _build_model_default(self):
        """写作领域的默认模型（无 owner/key 时）。"""
        return build_writer_model(self.settings)

    def _build_model_with_key(self, key: str, base_url: str | None, model_name: str | None):
        """按 owner 的 key 构建写作模型（多用户隔离 D9）。"""
        return build_writer_model(
            self.settings, api_key=key, base_url=base_url, model_name_override=model_name,
        )

    def _resolve_style_for_subagent(self, workspace_id: str, style_key: str, owner_id: str | None = None) -> str | None:
        """从激活风格中提取指定子代理对应的风格文本（SUFFIX）。

        Args:
            workspace_id: 工作区 ID
            style_key:    风格字段名（storybuilding_style / detail_outline_style / writing_style）
            owner_id:     所有者 ID（多用户隔离，None 回退管理员兜底）

        Returns:
            风格 SUFFIX 文本，无激活风格或该字段为空时返回 None
        """
        style_id = self.style_store.get_active_style_id(owner_id or "", workspace_id)
        if not style_id:
            return None
        style = self.style_store.get_style(owner_id or "", style_id)
        if not style:
            return None
        text = style.get(style_key, "")
        return text.strip() if text else None

    def _resolve_meta_style(self, workspace_id: str, owner_id: str | None = None) -> str | None:
        style_id = self.style_store.get_active_style_id(owner_id or "", workspace_id)
        if not style_id:
            return None
        style = self.style_store.get_style(owner_id or "", style_id)
        if not style:
            return None
        return style.get("meta_style") or None

    def _agent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, workspace_id: str | None = None, *, model=None, checkpointer=None, owner_id: str | None = None):
        # 多用户隔离（T2.4/T2.5）：model 用用户解密 key 构建，
        # checkpointer 用用户的分库 saver。两者外部注入，缺省回退全局（管理员兜底）。
        # Phase 7 包化重构：装配逻辑从 evolution 拉 manifest 改为同进程 import Agent 包。
        # 包自带 assemble(ctx)，执行端只构建 RuntimeContext 传入（D1=B / D8=X）。
        return self._assemble_via_package(
            workspace_path, trace_id, workspace_id,
            model=model, checkpointer=checkpointer, owner_id=owner_id,
        )

    def _assemble_via_package(
        self,
        workspace_path: Path,
        trace_id: str | None = None,
        workspace_id: str | None = None,
        *,
        model=None,
        checkpointer=None,
        owner_id: str | None = None,
    ):
        """Phase 7：经 Agent 包装配（替代 _assemble_via_manifest）。

        import Agent 包 → 构建 RuntimeContext（含 styles）→ package.assemble(ctx)。
        执行端只负责构建 model/backend/checkpointer + 解析 styles，装配全在包内。

        风格注入（D2/D4）：从 styling store 解析当前 workspace 的激活风格，
        按 scope 名填充 ctx.styles（包内 assemble 消费，注入各 subagent prompt）。
        scope→字段名映射就地处写（D4 决策）：
          meta → meta_style, storybuilding → storybuilding_style,
          detail-outline → detail_outline_style, writing → writing_style。
        """
        from contracts.runtime_context import RuntimeContext
        from app.platform.agent.loader import load_current_package
        from app.domains.writing.models import build_writer_model
        from app.platform.agent.middleware import TraceMiddleware
        from app.platform.credits.middleware import CreditsMiddleware

        # 积分制：尝试取全局 CreditsService（AD2）。失败/A/B 路径传 None（不计费）。
        credits_service = None
        try:
            from app.platform.credits.service import get_credits_service
            credits_service = get_credits_service()
        except Exception:
            pass

        if model is None:
            model = build_writer_model(self.settings)
        if checkpointer is None:
            checkpointer = self.checkpointer

        backend = self._backend_for_workspace(workspace_path)

        # 风格解析（D4 就地转换：scope 名 key，styling 字段名查值）
        # 无激活风格时 styles=None，包内用裸 prompt（无 suffix）。
        styles: dict[str, str] | None = None
        if workspace_id:
            style_map = {
                "meta": ("meta_style", None),  # (字段名, subagent style_key)，meta 无 subagent key
                "storybuilding": ("storybuilding_style", "storybuilding_style"),
                "detail-outline": ("detail_outline_style", "detail_outline_style"),
                "writing": ("writing_style", "writing_style"),
            }
            resolved: dict[str, str] = {}
            meta_style = self._resolve_meta_style(workspace_id, owner_id)
            if meta_style:
                resolved["meta"] = meta_style
            for scope, (_, sub_key) in style_map.items():
                if sub_key is None:
                    continue
                suffix = self._resolve_style_for_subagent(workspace_id, sub_key, owner_id)
                if suffix:
                    resolved[scope] = suffix
            styles = resolved or None

        ctx = RuntimeContext(
            model=model,
            backend=backend,
            checkpointer=checkpointer,
            workspace_path=workspace_path,
            trace_id=trace_id,
            owner_id=owner_id,
            styles=styles,
            trace_recorder=self.trace_recorder,
            trace_middleware_cls=TraceMiddleware,  # T2：类由执行端注入，包内实例化
            credits_service=credits_service,  # AD2：积分制服务，None 不计费
            credits_middleware_cls=CreditsMiddleware,  # AD6：类由执行端注入，包内实例化
            # NWM 记忆系统：per-workspace backend（workspace_id 定位 memory.db）。
            # pool 未初始化或配置缺失时返回 None → writing 走 ContextAssembler 全量注入。
            memory_backend=get_memory_backend(f"{owner_id}_{workspace_path.name}"),
            memory_recall_middleware_cls=_get_memory_recall_cls(),  # 从 harness 包加载
        )

        pkg = load_current_package()
        return pkg.assemble(ctx)

    # get_thread_checkpoint 复用 BaseAgentService 基类实现（PR-10 已提取，
    # 含 _normalize_message 规范化）。本地重复的 override + _normalize_message
    # / _map_role 已删除，消除与 base_service.py 的重复定义。

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
                model=model, owner_id=owner_id,
            )
            # T13：记录主控 prompt 版本到 trace（供后期 badcase 回放对照）。
            if hasattr(self, "_current_prompt_version"):
                self.trace_recorder.set_prompt_version(
                    trace.trace_id, "meta_system", self._current_prompt_version
                )
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={
                    "configurable": {"thread_id": thread.thread_id},
                    "callbacks": [TraceCallbackHandler(self.trace_recorder, trace.trace_id)],
                    "recursion_limit": 300,
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

    async def generate_stream(self, payload: ScreenplayGenerateRequest, thread: ThreadSummary, *, owner_id: str | None = None, run_purpose: str = "user_generation") -> AsyncIterator[str]:
        """Stream agent execution events as SSE to the frontend.

        SSE 编排骨架（心跳 + astream_events 循环 + interrupt 检测）由
        ``platform.streaming.run_agent_stream`` 提供，事件分发逻辑通过
        闭包 sink 注入（领域专属：model_output/tool_call/task 派发/章节计数）。

        run_purpose（Phase 3 T3.1）：A/B 回放传 "optimization"，写进 trace 的
        run_start.input，供监测层防自指断路（optimization trace 不进优化池）。
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
            trace = self.trace_recorder.create_run(thread, "screenplay.generate.stream", run_purpose=run_purpose)
            is_new = True
        if is_new:
            yield _sse("status", {"status": "started", "trace_id": trace.trace_id})
        trace_queue = self.trace_recorder.get_active_queue(trace.trace_id)
        if trace_queue is None:
            raise RuntimeError(f"Trace queue was not created: {trace.trace_id}")
        # 登记 SSE 生成器 task，让 POST /stop 能跨请求 task.cancel()（弥补浏览器
        # 刷新/关闭/cloudflared 掐断后 abortController 丢失导致后台 trace 停不掉的缺口）。
        # 清理由下方 try/finally 保证（正常/异常/CancelledError 三路都 unregister）。
        self.trace_recorder.register_run_task(trace.trace_id, asyncio.current_task())
        for trace_update in self._trace_updates(trace_queue):
            yield trace_update

        agent = self._agent_for_workspace(
            Path(thread.workspace_path), trace.trace_id, thread.workspace_id,
            model=model, checkpointer=checkpointer, owner_id=owner_id,
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
            "recursion_limit": 300,
        }

        # trace_pump 作为额外并发任务（与 agent 事件/心跳公平竞争 asyncio.wait）
        def _on_trace_done(finished: asyncio.Task) -> tuple[asyncio.Task | None, list[str]]:
            update = finished.result()
            return asyncio.create_task(_next_trace_update(trace_queue)), [update] if update else []

        trace_extra = ExtraTask(
            task=asyncio.create_task(_next_trace_update(trace_queue)),
            on_done=_on_trace_done,
        )

        # 领域事件分发：章节号/字数/task 派发等 writing 专属逻辑封装在 WritingEventSink（M4 抽离）
        sink = WritingEventSink(thread)

        sse_iter, result = await run_agent_stream(
            agent, agent_input, config,
            sink=sink,
            extra_tasks=[trace_extra],
        )

        try:
            async for frame in sse_iter:
                yield frame

            # ── 流结束：trace drain + interrupt 检测 + final ──────────────────────
            for trace_update in self._trace_updates(trace_queue):
                yield trace_update

            # T6.2：正常完成时结算预扣（多退少补 + 落流水 D23）
            self._settle_credits_if_any(thread.thread_id, force_stopped=False)

            if result.pending_interrupt is not None:
                iv = result.pending_interrupt
                # HITL：interrupt 命中 → 标记 trace awaiting_input 并落盘（D1）。
                # domain 层决定调用时机；recorder 写 run_awaiting + 更新 index +
                # notify evolution（不清内存，等 Command(resume) 续接）。
                self.trace_recorder.await_input_run(thread, trace.trace_id)
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
            # D4/D5/D6：CancelledError 三路分流。
            # - user_stop：用户点了停止按钮（_user_stop_requested 标记命中）→ cancelled
            # - awaiting_input：interrupt 期间连接断开 → 保持 awaiting_input 不收尾（可 resume）
            # - 否则：running 期间连接断开 → cancelled（agent 被 finally 真中止不可恢复）
            if self.trace_recorder.is_user_stop_requested(trace.trace_id):
                self.trace_recorder.cancel_run(thread, trace.trace_id, reason="user_stop")
                # 用户停止也要结算预扣（已消耗的 token 不退）
                self._settle_credits_if_any(thread.thread_id, force_stopped=False)
            elif self.trace_recorder.is_awaiting_input(trace.trace_id):
                pass  # interrupt 期间断连：保持 awaiting_input，等用户 resume
            else:
                self.trace_recorder.cancel_run(thread, trace.trace_id, reason="client_disconnect")
                self._settle_credits_if_any(thread.thread_id, force_stopped=False)
            raise
        except Exception as exc:
            # T6.1/T6.3：CreditExhaustedError 专门处理（D27 强停）
            from app.platform.credits.exceptions import CreditExhaustedError
            if isinstance(exc, CreditExhaustedError):
                self.trace_recorder.cancel_run(thread, trace.trace_id, reason="credit_stop")
                # T6.2：强停时结算预扣（force_stopped=True）
                self._settle_credits_if_any(thread.thread_id, force_stopped=True)
                for trace_update in self._trace_updates(trace_queue):
                    yield trace_update
                yield _sse("credit_exhausted", {
                    "message": str(exc),
                    "thread_id": thread.thread_id,
                })
                return
            self.trace_recorder.fail_run(thread, trace.trace_id, exc)
            for trace_update in self._trace_updates(trace_queue):
                yield trace_update
            raise
        finally:
            # 兜底清理 task 注册（正常/异常/CancelledError 三路都走到）。
            # _cleanup_run_state 也会兜底，但这里更早执行避免泄漏窗口。
            self.trace_recorder.unregister_run_task(trace.trace_id)

    def _trace_updates(self, trace_queue) -> list[str]:
        trace_events = _drain_trace_queue(trace_queue)
        if not trace_events:
            return []
        return [_sse("trace_event", trace_event) for trace_event in trace_events]

    def _settle_credits_if_any(self, thread_id: str, *, force_stopped: bool) -> None:
        """T6.2：结算当前 thread 的活跃预扣（如有）。

        正常完成 / 用户停止 / 客户端断连时调 force_stopped=False。
        CreditExhaustedError 强停时调 force_stopped=True。
        无活跃预扣时静默跳过（interview 阶段 / 非计费路径）。
        """
        try:
            from app.platform.credits.service import get_credits_service
            svc = get_credits_service()
            hold = svc.get_active_hold(thread_id)
            if hold:
                svc.settle_hold(hold["hold_id"], force_stopped=force_stopped)
        except RuntimeError:
            pass  # CreditsService 未初始化（A/B 测试 / 管理员路径），跳过
        except Exception:
            pass  # 结算失败不影响创作主流程（已落盘的内容不丢失）

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


async def _next_trace_update(queue) -> str:
    event = await queue.get()
    return _sse("trace_event", event.model_dump())


def _drain_trace_queue(queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait().model_dump())
    return events
