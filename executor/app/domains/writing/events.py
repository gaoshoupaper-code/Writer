"""WritingEventSink — writing domain 专属 SSE 事件处理器（M4 抽离自 agent.py）。

实现 platform.streaming.EventSink 协议，处理 LangGraph astream_events 产出的事件，
转换为前端 SSE 帧（model_output / tool_call / tool_output / tool_error / model_stream）。

与 image domain 的 _ImageSink 对称：各 domain 自带事件分发逻辑，SSE 骨架
（run_agent_stream 心跳/多路复用/interrupt 检测）在 platform.streaming 共用。

设计（D5 有状态对象 + D8 副作用抽私有方法）：
- 构造时接收 thread，内部持有 active_tasks（task 元信息）/ subagent_call_counts（调用计数）。
- on_event 既做事件转换（A 类），也在 task 结束时触发后端副作用（B 类）：
    - storybuilding 完成 → 派生流程图（写盘，_on_storybuilding_done）
    - writing 章节完成 → 算字数塞 tool_output（回流 SSE，_on_writing_chapter_done）
- 副作用抽成私有方法，缓解 on_event 的可测试性（可单独 mock 验证）。

为什么副作用留在 sink 而非 middleware（D8 验证结论）：
  middleware 的 wrap_tool_call 拦截单个工具（read_file 等），抓不到 DeepAgents
  的 subagent task 调度事件（task 是框架内部路由）。流程图生成/字数统计依赖
  "某 subagent 完成"的语义，只能在 astream_events 层（sink）捕获。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from app.domains.writing.expert_agent.services.storyline_graph import generate_storyline_graph
from app.platform.streaming import sse as _sse


# ======================================================================
# 焦点信息辅助（D6/D7）：章节号正则 + 总章数 + 字数，纯后端计算
# ======================================================================

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


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
    m = re.search(r"第\s*([0-9一二三四五六七八九十百]+)\s*章", description)
    if m:
        return _cn_or_int(m.group(1))
    m = re.search(r"chapter[\s\-_]*([0-9]+)", description, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _count_total_chapters(workspace_path: Path) -> int | None:
    """总章数 = detail/chapter-*.md 的最大编号（D6）。"""
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


def _extract_subagent_name(args: object) -> str:
    """从 task() 工具参数提取 subagent 名。"""
    if isinstance(args, dict):
        return str(args.get("subagent_type") or args.get("name") or "unknown")
    return "unknown"


def _extract_tool_call_id(data: dict) -> str | None:
    """从 on_tool_end 事件的 data 取 tool call ID。"""
    if not isinstance(data, dict):
        return None
    inp = data.get("input")
    if isinstance(inp, dict):
        cid = inp.get("id") or inp.get("call_id")
        if cid:
            return str(cid)
    return data.get("call_id")


def _current_parent_task(active_tasks: dict[str, dict]) -> tuple[str | None, str | None]:
    """返回最近活跃 SubAgent task 的 (call_id, name)。"""
    if not active_tasks:
        return None, None
    last_id = next(reversed(active_tasks))
    return last_id, active_tasks[last_id]["name"]


def _extract_tool_calls(message: object) -> list[dict]:
    """从 chat model 输出消息提取 tool_call 信息。"""
    calls = []
    if hasattr(message, "tool_calls"):
        for tc in message.tool_calls:
            calls.append({"name": tc.get("name", ""), "args": tc.get("args", {})})
    elif isinstance(message, dict):
        for tc in message.get("tool_calls", []):
            calls.append({"name": tc.get("name", ""), "args": tc.get("args", {})})
    return calls


def _extract_model_text(message: object) -> str:
    """从 chat model 输出消息提取文本内容。"""
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


# ======================================================================
# WritingEventSink
# ======================================================================


class WritingEventSink:
    """writing domain 的 SSE 事件处理器（EventSink 协议实现）。

    有状态（D5）：构造时接收 thread，内部维护 task 调度元信息。
    请求级隔离：每次 generate_stream 新建实例。
    """

    def __init__(self, thread) -> None:
        self._thread = thread
        self._workspace_path = Path(thread.workspace_path)
        self._active_tasks: dict[str, dict] = {}
        self._subagent_call_counts: dict[str, int] = {}

    async def on_event(self, event: dict) -> list[str]:
        """处理一个 agent 事件，返回要 yield 的 SSE 帧（可空列表）。"""
        frames: list[str] = []
        kind = event["event"]
        name = event.get("name", "")
        data = event.get("data", {})

        if kind == "on_chat_model_end":
            output = data.get("output")
            tool_calls = _extract_tool_calls(output)
            text = _extract_model_text(output)
            evt_data: dict[str, Any] = {"text": text}
            if tool_calls:
                evt_data["tool_calls"] = tool_calls
            frames.append(_sse("model_output", evt_data))

        elif kind == "on_chain_start" and name == "tools":
            frames.extend(await self._handle_tool_start(data))

        elif kind == "on_tool_end":
            frames.extend(await self._handle_tool_end(name, data))

        elif kind == "on_tool_error":
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
            # T20: 转发 reasoning token（DeepSeek thinking 模式的思考链）。
            # DeepSeekThinkingChatModel 已在流式 chunk 的 additional_kwargs["reasoning_content"]
            # 上携带逐 token reasoning delta（deepseek_thinking.py:112-114）。
            # 非 deepseek 模型不产出此字段，自动降级为只推 model_stream。
            reasoning = ""
            if hasattr(chunk, "additional_kwargs"):
                reasoning = str(chunk.additional_kwargs.get("reasoning_content", "") or "")
            elif isinstance(chunk, dict):
                msg = chunk.get("message") or chunk
                if isinstance(msg, dict):
                    ak = msg.get("additional_kwargs") or {}
                    reasoning = str(ak.get("reasoning_content", "") or "")
            if reasoning:
                frames.append(_sse("reasoning_stream", {"content": reasoning}))

        return frames

    # ── task 工具开始：派发焦点信息 + 维护元信息 ──────────────────

    async def _handle_tool_start(self, data: dict) -> list[str]:
        """处理 on_chain_start(tools)：派发 tool_call 帧（task 含章节号/总章数）。"""
        frames: list[str] = []
        tool_inputs = data.get("input", [])
        parent_task_id, subagent_name = _current_parent_task(self._active_tasks)
        for tc in tool_inputs:
            if not isinstance(tc, dict):
                continue
            tool_name = tc.get("name", "unknown")
            call_id = tc.get("id", "")
            call_payload: dict[str, Any] = {
                "tool": tool_name,
                "input": tc.get("args", {}),
                "call_id": call_id,
                "parent_task_id": parent_task_id,
                "subagent_name": subagent_name,
            }
            if tool_name == "task":
                args = tc.get("args", {}) or {}
                sub = _extract_subagent_name(args)
                description = str(args.get("description", "") or "")
                self._subagent_call_counts[sub] = self._subagent_call_counts.get(sub, 0) + 1
                call_ordinal = self._subagent_call_counts[sub]
                chapter_index = _extract_chapter_index(description)
                total_chapters = await asyncio.to_thread(_count_total_chapters, self._workspace_path)
                # writing 章节号降级：正则失败时按 writing 调用序推断（D6）
                if sub == "writing" and chapter_index is None:
                    chapter_index = call_ordinal
                if call_id:
                    self._active_tasks[call_id] = {
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
        return frames

    # ── task 工具结束：副作用（流程图/字数）+ tool_output 帧 ────────

    async def _handle_tool_end(self, name: str, data: dict) -> list[str]:
        """处理 on_tool_end：产出 tool_output 帧，task 结束时触发领域副作用（D8）。"""
        output_payload: dict[str, Any] = {
            "tool": name,
            "output": str(data.get("output", ""))[:2000],
            "call_id": _extract_tool_call_id(data),
        }
        if name == "task":
            await self._on_task_completed(data, output_payload)
        return [_sse("tool_output", output_payload)]

    async def _on_task_completed(self, data: dict, output_payload: dict[str, Any]) -> None:
        """task 完成：按 subagent 类型触发领域副作用（D8 B 类）。

        - storybuilding 完成 → 派生流程图（写盘，失败吞掉不阻断 SSE）。
        - writing 章节完成 → 算字数塞进 output_payload（回流 SSE 帧）。
        """
        call_id = _extract_tool_call_id(data)
        if not call_id or call_id not in self._active_tasks:
            return
        task_meta = self._active_tasks.pop(call_id)
        finished_subagent = task_meta.get("name")

        if finished_subagent == "storybuilding":
            await self._on_storybuilding_done()
        elif finished_subagent == "writing":
            chapter_index = task_meta.get("chapter_index")
            if chapter_index:
                await self._on_writing_chapter_done(chapter_index, output_payload)

    async def _on_storybuilding_done(self) -> None:
        """storybuilding 完成后派生流程图（写盘副作用）。

        确定性、纯后端。失败已被 generate_storyline_graph 内部 try/except 吞掉，
        这里只读不抛，绝不阻断 SSE 流。
        """
        await asyncio.to_thread(generate_storyline_graph, self._workspace_path)

    async def _on_writing_chapter_done(self, chapter_index: int, output_payload: dict[str, Any]) -> None:
        """writing 章节完成后算字数，塞进 output_payload（D7，随 tool_output 推前端）。"""
        word_count = await asyncio.to_thread(_count_chapter_words, self._workspace_path, chapter_index)
        if word_count is not None:
            output_payload["word_count"] = word_count
            output_payload["chapter_index"] = chapter_index


__all__ = ["WritingEventSink"]
