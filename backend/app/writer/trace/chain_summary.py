"""chain_summary 生成逻辑 — 为 TraceNode 生成链路视图的行内摘要。"""

from __future__ import annotations

from typing import Any, Callable

from .schemas import TraceLogEvent, TraceRunSummary, TraceTodoItem


# ── Tool 结构化摘要映射 (D7) ──


def _extract_path(output: Any) -> str:
    """从 tool output 中提取文件路径。"""
    if isinstance(output, str):
        return output[:60]
    if isinstance(output, dict):
        # 常见结构：{"path": "...", "content": "..."} 或 args.path
        for key in ("path", "file_path", "filename"):
            value = output.get(key)
            if isinstance(value, str):
                return value
        # DeepAgent tool output: {"content": "..."} 中可能没有 path
        content = output.get("content")
        if isinstance(content, str):
            return content[:60]
    return str(output)[:60]


def _extract_goal(output: Any) -> str:
    """从 set_goal 的 output 中提取目标文本。"""
    if isinstance(output, dict):
        for key in ("goal", "content", "text"):
            value = output.get(key)
            if isinstance(value, str):
                return value
    return str(output)


def _extract_completed(output: Any) -> str:
    """从 record_goal_completion 的 output 中提取完成信息。"""
    if isinstance(output, dict):
        for key in ("goal", "content", "completed"):
            value = output.get(key)
            if isinstance(value, str):
                return value[:50]
    return str(output)[:50]


TOOL_SUMMARY_BUILDERS: dict[str, Callable[[Any], str]] = {
    "write_file": lambda out: f"write_file: {_extract_path(out)}",
    "write": lambda out: f"write_file: {_extract_path(out)}",
    "read_file": lambda out: f"read_file: {_extract_path(out)}",
    "read": lambda out: f"read_file: {_extract_path(out)}",
    "set_goal": lambda out: f"set_goal: {_extract_goal(out)[:50]}",
    "record_goal_completion": lambda out: f"goal_completion: {_extract_completed(out)}",
    "update_todo_list": lambda _out: "update_todo_list",
}


def build_tool_summary(tool_name: str | None, tool_output: Any) -> str:
    """生成 tool 节点的 chain_summary。"""
    if not tool_name:
        return f"Tool: {str(tool_output)[:80]}"
    builder = TOOL_SUMMARY_BUILDERS.get(tool_name)
    if builder:
        return builder(tool_output)
    return f"{tool_name}"


# ── LLM output 纯文本提取 ──


def _extract_llm_text(output: Any) -> str:
    """从 LLM output 中提取纯文本，取前 100 字符。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        # 常见结构：{"messages": [...]} 或直接有 content
        messages = output.get("messages")
        if isinstance(messages, list):
            # 取最后一条 AI 消息的 content
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("type") or msg.get("role") or ""
                if role in ("ai", "assistant"):
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        # multimodal content blocks
                        parts: list[str] = []
                        for block in content:
                            if isinstance(block, dict):
                                text = block.get("text")
                                if isinstance(text, str):
                                    parts.append(text)
                            elif isinstance(block, str):
                                parts.append(block)
                        return "\n".join(parts)
        content = output.get("content")
        if isinstance(content, str):
            return content
    return str(output)


# ── 各类型节点的 chain_summary 生成 ──


def run_summary(run: TraceRunSummary) -> str:
    """生成 run 节点的 chain_summary。"""
    status_label = {"completed": "完成", "failed": "失败", "running": "运行中"}.get(run.status, run.status)
    duration = f"{run.duration_ms / 1000:.1f}s" if run.duration_ms is not None else "--"
    return f"{run.endpoint} · {status_label} · {duration}"


def agent_summary(event: TraceLogEvent) -> str:
    """生成 agent 节点的 chain_summary。"""
    agent_name = event.agent_name or "Unknown"
    role = "子代理" if (event.agent_name or "").endswith("-subagent") else "主代理"
    return f"{agent_name} · {role}"


def llm_summary(event: TraceLogEvent, is_running: bool = False) -> str:
    """生成 LLM 节点的 chain_summary。"""
    model = event.model_name or "LLM"
    if is_running:
        return f"{model}: 运行中…"
    text = _extract_llm_text(event.output)
    if text:
        return f"{model}: {text[:100]}{'…' if len(text) > 100 else ''}"
    # content 为空但有 tool_calls → 直接列工具名
    tool_calls = event.tool_calls
    if isinstance(tool_calls, list) and tool_calls:
        names: list[str] = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                name = tc.get("name", "")
                if name:
                    names.append(str(name))
        if names:
            return f"{model}: {', '.join(names)}"
    return f"{model}: (无输出)"


def todo_summary(items: list[TraceTodoItem]) -> str:
    """生成 todo 节点的 chain_summary。"""
    if not items:
        return "0 个任务"
    active = None
    for item in items:
        if item.status == "in_progress":
            active = item.content
            break
    if active:
        return f"{len(items)} 个任务，当前: {active}"
    return f"{len(items)} 个任务"


def error_summary(error: str | None, tool_output: Any = None) -> str:
    """生成 error 节点的 chain_summary。"""
    if error:
        return f"❌ {error[:200]}"
    if tool_output is not None:
        return f"❌ {str(tool_output)[:200]}"
    return "❌ 未知错误"


def skill_summary(skill_name: str) -> str:
    """生成 skill 节点的 chain_summary。"""
    return f"📖 {skill_name}"
