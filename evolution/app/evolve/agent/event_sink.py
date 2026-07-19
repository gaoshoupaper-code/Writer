"""EvolveEventSink — 进化 Agent 专属 SSE 事件处理器（Phase 2C，决策 T4）。

实现与 executor.platform.streaming.EventSink 对称的协议，处理 LangGraph
astream_events 产出的事件，转换为进化端 SSE 帧。

Phase 2C：本文件已就绪但未被 round 函数接入（ainvoke → astream 改造留 Phase 3
与 API/SSE 重构一起做，避免过渡期双轨）。

SSE 事件类型（决策 T4）：
  基础事件（与 writer 端对齐）：
    - model_output      Agent 一轮回复完整文本（on_chat_model_end）
    - model_stream      Agent 逐 token 增量（on_chat_model_stream，前端打字机效果）
    - tool_call         工具调用开始（on_chain_start(tools)）
    - tool_output       工具调用结果（on_tool_end）
    - tool_error        工具调用错误（on_tool_error）

  进化专属事件（决策 B 双轨制 + 浮窗实时同步）：
    - proposal          进化点状态变更（监听 propose/update/reject 工具调用）

  阶段/进度事件（决策 W）：
    - phase             阶段切换（inspect → conversing → finalizing）
    - finalizing        落地进度（edit/validate/change_log）

设计：进化端比 writer 简单——无 subagent / 章节统计 / 流程图等业务副作用，
sink 只做"事件 → SSE 帧"转换，无后端副作用。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("evolution.evolve.agent.event_sink")


# ── SSE 帧构造 helper（复制自 executor.platform.streaming，避免跨包依赖）──


def sse(event_type: str, payload: object) -> str:
    """构造标准 SSE 帧：event: <type>\ndata: <json>\n\n。

    payload 用 json.dumps 序列化，ensure_ascii=False 保留中文，
    default=str 兜底不可序列化对象。
    """
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def heartbeat() -> str:
    """SSE 心跳注释行（浏览器忽略，保持连接活跃）。"""
    return ": ping\n\n"


# ── 进化点工具名集合（用于识别 proposal 事件）──────────────────────
_PROPOSAL_TOOLS = frozenset({
    "propose_evolution_point",
    "update_evolution_point",
    "reject_evolution_point",
})


class EvolveEventSink:
    """进化 Agent 的 SSE 事件处理器（决策 T4）。

    实现 EventSink 协议的 on_event 方法，处理 LangGraph astream_events 事件。
    Phase 3 接入时由 run_agent_stream 调用（参考 executor 端 run_agent_stream 骨架）。

    进化端 sink 比 writer 端简单：无业务副作用，只做事件 → SSE 转换。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # 跟踪进行中的 tool call id → name（on_tool_end 时反查 name）
        self._active_tools: dict[str, str] = {}

    async def on_event(self, event: dict) -> list[str]:
        """处理一个 astream_events 事件，返回要 yield 的 SSE 帧列表。

        与 executor.platform.streaming.EventSink 协议对齐——返回 SSE 帧字符串。

        Args:
            event: LangGraph astream_events(version="v2") 产出的 event dict，
                   含 event/name/data/run_id 等字段。

        Returns:
            SSE 帧字符串列表（可空——某些事件不产出帧）。
        """
        dicts = await self.on_event_dicts(event)
        return [sse(d["type"], {k: v for k, v in d.items() if k != "type"}) for d in dicts]

    async def on_event_dicts(self, event: dict) -> list[dict]:
        """处理一个 astream_events 事件，返回结构化帧 dict 列表（Phase 6）。

        与 on_event 的区别：返回 dict（含 type 字段）而非 SSE 帧字符串。
        进化端 _run_agent_streamed 用此方法，把 dict 通过 recorder 桥接到 SSE 端，
        SSE 端的 _trace_event_to_sse 识别 type 字段派生最终 SSE 帧。

        这样保持「按需触发模型」（Agent 在后台 task 跑）+「token 级流式」兼容。

        Returns:
            [{"type": "model_output"|"model_stream"|"tool_call"|..., ...}, ...]
        """
        frames: list[dict] = []
        kind = event.get("event", "")
        name = event.get("name", "")
        data = event.get("data", {}) or {}

        try:
            if kind == "on_chat_model_end":
                frames.extend(self._handle_model_end(data))

            elif kind == "on_chat_model_stream":
                frames.extend(self._handle_model_stream(data))

            elif kind == "on_chain_start" and name == "tools":
                frames.extend(self._handle_tools_start(data))

            elif kind == "on_tool_end":
                frames.extend(self._handle_tool_end(name, data))

            elif kind == "on_tool_error":
                frames.extend(self._handle_tool_error(name, data))

        except Exception:
            # sink 内部异常不应中断 agent 流——记日志继续
            logger.exception(
                "EvolveEventSink 处理事件异常: session=%s kind=%s name=%s",
                self.session_id, kind, name,
            )

        return frames

    # ── 基础事件处理（Phase 6：返回 dict 列表，含 type 字段）─────

    def _handle_model_end(self, data: dict) -> list[dict]:
        """on_chat_model_end：Agent 一轮回复完整文本 + 工具调用意图。

        产出 model_output 帧（含 text + tool_calls）。
        """
        output = data.get("output")
        text = _extract_model_text(output)
        tool_calls = _extract_tool_calls(output)

        evt: dict[str, Any] = {"type": "model_output", "text": text}
        if tool_calls:
            evt["tool_calls"] = tool_calls
        return [evt]

    def _handle_model_stream(self, data: dict) -> list[dict]:
        """on_chat_model_stream：逐 token 增量（前端打字机效果）。

        产出 model_stream 帧（只含 content delta）。
        """
        chunk = data.get("chunk")
        if hasattr(chunk, "content"):
            content = chunk.content
        elif isinstance(chunk, dict):
            content = chunk.get("content", "")
        else:
            content = ""

        if not content or not isinstance(content, str):
            return []

        return [{"type": "model_stream", "content": content}]

    def _handle_tools_start(self, data: dict) -> list[dict]:
        """on_chain_start(tools)：工具调用开始。

        产出 tool_call 帧（tool/input/call_id）。
        进化点工具额外产出 proposal 帧（决策 B 双轨制浮窗同步）。
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

            # 记录进行中的工具（on_tool_end 时反查 name）
            if call_id:
                self._active_tools[call_id] = tool_name

            frames.append({
                "type": "tool_call",
                "tool": tool_name,
                "input": _summarize_tool_args(tool_name, args),
                "call_id": call_id,
            })

            # 进化点工具：额外产 proposal 帧（浮窗实时同步）
            if tool_name in _PROPOSAL_TOOLS:
                frames.append({
                    "type": "proposal",
                    "action": _proposal_action(tool_name),
                    "tool": tool_name,
                    "args_summary": _summarize_proposal_args(tool_name, args),
                    "call_id": call_id,
                })

        return frames

    def _handle_tool_end(self, name: str, data: dict) -> list[dict]:
        """on_tool_end：工具调用完成，产出结果摘要。

        产出 tool_output 帧（tool/output_summary/call_id）。
        结果只取摘要（避免大块输出拥堵 SSE，决策 R2）。
        """
        output = data.get("output")
        output_str = _extract_tool_output_text(output)
        call_id = _extract_tool_call_id(data)

        # 清理进行中跟踪
        if call_id and call_id in self._active_tools:
            self._active_tools.pop(call_id, None)

        return [{
            "type": "tool_output",
            "tool": name,
            "output_summary": output_str[:500],  # 截断防 SSE 拥堵（决策 R2）
            "call_id": call_id,
        }]

    def _handle_tool_error(self, name: str, data: dict) -> list[dict]:
        """on_tool_error：工具调用错误。

        产出 tool_error 帧（含 error 信息）。
        """
        err = data.get("error") or data.get("output") or ""
        call_id = _extract_tool_call_id(data)
        return [{
            "type": "tool_error",
            "tool": name,
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
    # LangGraph astream_events v2 的 on_tool_end data 含 output（ToolMessage）
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
    """工具参数摘要（避免大块 args 拥堵 SSE）。

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


def _proposal_action(tool_name: str) -> str:
    """进化点工具 → 浮窗 action 标签。"""
    return {
        "propose_evolution_point": "propose",
        "update_evolution_point": "update",
        "reject_evolution_point": "reject",
    }.get(tool_name, "unknown")


def _summarize_proposal_args(tool_name: str, args: dict) -> dict[str, Any]:
    """进化点工具调用的参数摘要（浮窗联动用）。

    propose/update/reject 的关键字段抽取，供前端浮窗即时显示
    （完整数据仍以 GET /points 接口为准——这是双轨制的"快预览"）。
    """
    if not isinstance(args, dict):
        return {}
    if tool_name == "propose_evolution_point":
        return {
            "target": args.get("target", ""),
            "problem_excerpt": (args.get("problem", "") or "")[:100],
        }
    if tool_name == "update_evolution_point":
        return {
            "point_id": args.get("point_id", ""),
            "chosen_option": args.get("chosen_option"),
        }
    if tool_name == "reject_evolution_point":
        return {
            "point_id": args.get("point_id", ""),
            "reason_excerpt": (args.get("reason", "") or "")[:100],
        }
    return {}


__all__ = [
    "EvolveEventSink",
    "sse",
    "heartbeat",
]
