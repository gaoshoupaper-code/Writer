"""EvolveEventSink — 进化 Agent 专属事件处理器（trace 重构 20260720_154825）。

**重大变更**：移除 token 级流式（on_chat_model_stream），改为只消费轮次级事件。
trace 通道从此只存框架级 span + 业务 run_meta，token 流不再污染 event_payloads。

职责：
  消费 LangGraph astream_events 产出的事件，转换为：
    1. 结构化帧 dict（含 type 字段）—— 供 _run_agent_streamed 派生：
       - SSE 帧给前端（model_output / tool_call / tool_output）
       - 持久化消息（assistant / tool / system）到 evolve_messages
    2. 跟踪 LangGraph superstep 最大值（观测用）

事件类型（重构后）：
  - model_output   Agent 一轮回复完整文本 + 工具调用意图（on_chat_model_end）
  - tool_call      工具调用开始（on_chain_start(tools)）—— 仅当本轮有工具调用时
  - tool_output    工具调用结果（on_tool_end）
  - tool_error     工具调用错误（on_tool_error）

移除的事件（D3 决策）：
  - model_stream   逐 token 增量（on_chat_model_stream）—— 砍掉，token 不入 trace

设计原则（D1）：trace 通道职责单一，只存 span + run_meta；消息通道独立。
sink 只产结构化帧，不直接调 EvolveMessagesRepo（持久化由 _run_agent_streamed 协调）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("evolution.evolve.agent.event_sink")


# ── 进化点工具名集合（用于识别 proposal 事件）──────────────────────

_PROPOSAL_TOOLS = frozenset({
    "propose_evolution_point",
    "update_evolution_point",
    "reject_evolution_point",
})


class EvolveEventSink:
    """进化 Agent 的事件处理器（trace 重构后）。

    只消费轮次级事件（model_end / tool_end / tool_error），token 流（model_stream）
    不再处理。产出的结构化帧由 _run_agent_streamed 协调，派生为：
      - 前端 Pull 帧（通知前端刷新消息）
      - 持久化消息（落 evolve_messages）
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # 跟踪 LangGraph superstep 最大值（DD5）：从事件 metadata.langgraph_step 取，
        # session 结束时由 _run_agent_streamed 写 step_stats run_meta（DD8）。
        # 与 GraphRecursionError 的计数口径对齐，能直接回答"会不会触顶 200"。
        self.max_superstep = 0

    async def on_event_dicts(self, event: dict) -> list[dict]:
        """处理一个 astream_events 事件，返回结构化帧 dict 列表。

        Args:
            event: LangGraph astream_events(version="v2") 产出的 event dict。

        Returns:
            [{"type": "model_output"|"tool_call"|"tool_output"|"tool_error", ...}, ...]
            空列表表示该事件不产出帧（如 token 流 / 不关心的事件）。
        """
        frames: list[dict] = []
        kind = event.get("event", "")
        name = event.get("name", "")
        data = event.get("data", {}) or {}

        # 跟踪 LangGraph superstep 最大值（DD5）：每条 astream_events 都带 metadata，
        # 其中 langgraph_step 是当前 superstep 序号。取 max 用于观测实际步数。
        step = (event.get("metadata") or {}).get("langgraph_step")
        if isinstance(step, int) and step > self.max_superstep:
            self.max_superstep = step

        try:
            if kind == "on_chat_model_end":
                frames.extend(self._handle_model_end(data))

            elif kind == "on_chain_start" and name == "tools":
                frames.extend(self._handle_tools_start(data))

            elif kind == "on_tool_end":
                frames.extend(self._handle_tool_end(name, data))

            elif kind == "on_tool_error":
                frames.extend(self._handle_tool_error(name, data))

            # on_chat_model_stream（token 流）：D3 决策砍掉，不再处理。
            # 这样 trace 通道不再被 token 噪音污染，event_count 反映真实轮次数。

        except Exception:
            # sink 内部异常不应中断 agent 流——记日志继续
            logger.exception(
                "EvolveEventSink 处理事件异常: session=%s kind=%s name=%s",
                self.session_id, kind, name,
            )

        return frames

    # ── 轮次级事件处理（返回 dict 列表，含 type 字段）─────────────

    def _handle_model_end(self, data: dict) -> list[dict]:
        """on_chat_model_end：Agent 一轮回复完整文本 + 工具调用意图。

        产出 model_output 帧（含 text + tool_calls）。
        _run_agent_streamed 据此派生：
          - 无 tool_calls → 持久化 assistant 消息（开场白/对话回复）
          - 有 tool_calls → 不落 assistant 文本（工具单独走 tool 消息）
        """
        output = data.get("output")
        text = _extract_model_text(output)
        tool_calls = _extract_tool_calls(output)

        evt: dict[str, Any] = {"type": "model_output", "text": text}
        if tool_calls:
            evt["tool_calls"] = tool_calls
        return [evt]

    def _handle_tools_start(self, data: dict) -> list[dict]:
        """on_chain_start(tools)：工具调用开始。

        产出 tool_call 帧（tool_name/input/call_id）。
        注意：实际 tool_output 在 on_tool_end 才完整；这里只给"开始"信号，
        用于前端立即显示"Agent 在调 xxx 工具"（通过 Pull 拉到帧后刷新消息列表）。
        """
        frames: list[dict] = []
        tool_inputs = data.get("input", [])
        if not isinstance(tool_inputs, list):
            return frames

        for tc in tool_inputs:
            if not isinstance(tc, dict):
                continue
            tool_name = tc.get("name", "unknown")
            call_id = tc.get("id", "")
            args = tc.get("args", {}) or {}

            frames.append({
                "type": "tool_call",
                "tool_name": tool_name,
                "input": _summarize_tool_args(tool_name, args),
                "call_id": call_id,
            })

        return frames

    def _handle_tool_end(self, name: str, data: dict) -> list[dict]:
        """on_tool_end：工具调用完成，产出结果摘要。

        产出 tool_output 帧（tool_name/output_summary/call_id）。
        结果只取摘要（避免大块输出拥堵）。

        _run_agent_streamed 据此对落地/进化点工具派生 tool 消息持久化。
        """
        output = data.get("output")
        output_str = _extract_tool_output_text(output)
        call_id = _extract_tool_call_id(data)

        # 工具调用的"开始"信号已由 _handle_tools_start 产出 tool_call 帧；
        # 这里产出 tool_output 帧，让前端能区分"开始 / 完成"。
        return [{
            "type": "tool_output",
            "tool_name": name,
            "output_summary": output_str[:500],  # 截断防 Pull 帧过大
            "call_id": call_id,
        }]

    def _handle_tool_error(self, name: str, data: dict) -> list[dict]:
        """on_tool_error：工具调用错误。产出 tool_error 帧。"""
        err = data.get("error") or data.get("output") or ""
        call_id = _extract_tool_call_id(data)
        return [{
            "type": "tool_error",
            "tool_name": name,
            "call_id": call_id,
            "error": str(err)[:500],
        }]


# ── 辅助函数 ─────────────────────────────────────────────────


def _extract_model_text(output: Any) -> str:
    """从 on_chat_model_end 的 output 提取文本内容。

    支持 AIMessage / dict / str 三种格式。
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if hasattr(output, "content"):
        content = output.content
        return content if isinstance(content, str) else str(content)
    if isinstance(output, dict):
        return output.get("content", "") or ""
    return str(output)


def _extract_tool_calls(output: Any) -> list[dict[str, Any]]:
    """从 on_chat_model_end 的 output 提取工具调用意图。

    返回 [{name, args, id}, ...]。无工具调用返回空列表。
    """
    if output is None:
        return []
    raw_calls = None
    if hasattr(output, "tool_calls"):
        raw_calls = output.tool_calls
    elif isinstance(output, dict):
        raw_calls = output.get("tool_calls")

    if not raw_calls or not isinstance(raw_calls, list):
        return []

    result: list[dict[str, Any]] = []
    for tc in raw_calls:
        if not isinstance(tc, dict):
            continue
        result.append({
            "name": tc.get("name", ""),
            "args": tc.get("args", {}) or tc.get("input", {}),
            "id": tc.get("id", ""),
        })
    return result


def _extract_tool_call_id(data: dict) -> str:
    """从 on_tool_end/on_tool_error 的 data 提取 tool call id。"""
    output = data.get("output")
    if hasattr(output, "tool_call_id"):
        return str(output.tool_call_id) or ""
    if isinstance(output, dict):
        return str(output.get("tool_call_id", "")) or ""
    return ""


def _extract_tool_output_text(output: Any) -> str:
    """从 on_tool_end 的 output 提取工具结果文本。

    支持 ToolMessage（有 .content）/ dict / str 三种格式。
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if hasattr(output, "content"):
        content = output.content
        return content if isinstance(content, str) else str(content)
    if isinstance(output, dict):
        return output.get("content", "") or str(output)
    return str(output)


def _summarize_tool_args(tool_name: str, args: dict) -> dict[str, Any]:
    """工具参数摘要（避免大块 args 拥堵 Pull 帧）。

    长字段（如 content/code/changes_json）截断到 200 字符 + 标记 truncated。
    """
    if not isinstance(args, dict):
        return {}
    summary: dict[str, Any] = {}
    long_fields = {"content", "code", "changes_json", "applied_json", "options_json", "problem", "rationale", "summary"}
    for k, v in args.items():
        if k in long_fields and isinstance(v, str) and len(v) > 200:
            summary[k] = v[:200] + "...[truncated]"
        else:
            summary[k] = v
    return summary


__all__ = [
    "EvolveEventSink",
]
