"""ContextAssemblerMiddleware — 通用上下文组装中间件。

职责：
  由主代理配置文件路径列表，在第一次模型调用前从文件系统读取指定文件，
  将上下文作为 HumanMessage 永久注入到 state.messages。
  后续模型调用通过重排序保证上下文位于任务前面，确保缓存前缀稳定。

注入策略（双钩子协作）：
  1. before_model（一次性）：
     - 检测 state.messages 中是否已存在上下文消息
     - 首次调用时读取文件，构建上下文 HumanMessage，追加到 state.messages
     - 由于 add_messages reducer 只能 append，上下文会被追加到末尾
  2. wrap_model_call（每次）：
     - 将上下文消息从末尾移到列表开头（通过 request.override 临时重排序）
     - 模型始终看到 [上下文, 任务, ...] 的顺序
     - 缓存前缀 [上下文, 任务] 在整个调用过程中保持不变 → 命中缓存

文件路径配置：
  由主代理在构建子代理时通过 file_paths 参数指定。
  支持通配符模式（如 "character/*.md"、"detail/chapter-*.md"）。
  文件按列表顺序读取，确保 prompt caching 前缀稳定。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from typing_extensions import override

# 文件路径配置：静态列表或根据任务文本动态计算的回调函数
FilePaths = list[str] | Callable[[str], list[str]]


class ContextAssemblerMiddleware(AgentMiddleware):
    """通用上下文组装中间件。

    由主代理配置文件路径，在第一次模型调用前永久注入上下文到 state。
    后续调用通过 wrap_model_call 重排序，将上下文放在任务前面，保证缓存命中。

    file_paths 支持两种模式：
    - 静态列表：每次注入相同文件集，适用于 outline、detail-outline 等
    - 动态回调：根据任务文本动态计算文件列表，适用于 writing（需根据章节号选择不同文件）
    """

    def __init__(
        self,
        workspace_root: Path,
        file_paths: FilePaths,
        context_label: str = "生成前置上下文",
    ) -> None:
        """
        Args:
            workspace_root: 工作区根目录的绝对路径
            file_paths: 需要读取的文件路径列表（相对于工作区根目录）
                        支持通配符，如 "character/*.md"、"detail/chapter-*.md"
                        按列表顺序读取，确保 prompt caching 前缀稳定
                        也支持传入回调函数，接收任务文本，返回文件路径列表
            context_label: 上下文标签文本（默认 "生成前置上下文"）
        """
        self.workspace = workspace_root
        self._file_paths = file_paths
        self.context_label = context_label
        self._context_prefix = f"{context_label}："

    # ------------------------------------------------------------------
    # before_model: 一次性永久注入上下文到 state
    # ------------------------------------------------------------------

    @override
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        """在第一次模型调用前，将文件上下文永久注入到 state.messages。

        检测 state.messages 中是否已存在上下文消息：
        - 已存在（前缀匹配）：跳过，避免重复注入
        - 不存在：读取文件，构建上下文，作为 HumanMessage 追加到 state

        注意：add_messages reducer 只能 append，所以上下文会被放在末尾。
        实际的重排序（上下文移到 task 前面）由 wrap_model_call 负责。
        """
        messages = state.get("messages", [])
        if _find_context_index(messages, self._context_prefix) is not None:
            return None
        task = _extract_task(messages)
        context = self._build(task)
        if not context:
            return None
        return {"messages": [HumanMessage(content=context)]}

    @override
    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        """异步版本。逻辑与同步版本完全相同。"""
        messages = state.get("messages", [])
        if _find_context_index(messages, self._context_prefix) is not None:
            return None
        task = _extract_task(messages)
        context = self._build(task)
        if not context:
            return None
        return {"messages": [HumanMessage(content=context)]}

    # ------------------------------------------------------------------
    # wrap_model_call: 重排序消息，上下文放最前面
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        """将上下文 HumanMessage 移到消息列表开头，保证缓存前缀稳定。

        before_model 已将上下文追加到 state.messages 末尾，
        这里通过 request.override 将其移到开头，让模型看到 [上下文, 任务, ...] 的顺序。
        如果上下文已经在开头或不存在，直接透传。
        """
        idx = _find_context_index(request.messages, self._context_prefix)
        if idx is None or idx == 0:
            return handler(request)
        reordered = list(request.messages)
        reordered.insert(0, reordered.pop(idx))
        return handler(request.override(messages=reordered))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        """异步版本。逻辑与同步版本完全相同。"""
        idx = _find_context_index(request.messages, self._context_prefix)
        if idx is None or idx == 0:
            return await handler(request)
        reordered = list(request.messages)
        reordered.insert(0, reordered.pop(idx))
        return await handler(request.override(messages=reordered))

    # ------------------------------------------------------------------
    # 上下文组装 — 通用，基于配置的文件路径
    # ------------------------------------------------------------------

    def _resolve_file_paths(self, task_text: str) -> list[str]:
        """解析文件路径配置，支持静态列表和动态回调。"""
        if callable(self._file_paths):
            return self._file_paths(task_text)
        return self._file_paths

    def _build(self, task_text: str = "") -> str:
        """按配置的文件路径列表读取文件并组装上下文。

        Args:
            task_text: 当前任务文本，用于动态路径解析

        Returns:
            组装后的上下文字符串，如果所有文件都不存在则返回空字符串
        """
        file_paths = self._resolve_file_paths(task_text)
        blocks: list[str] = []
        for pattern in file_paths:
            block = self._resolve_pattern(pattern)
            if block:
                blocks.append(block)
        content = "\n\n".join(blocks)
        return f"{self.context_label}：\n{content}" if content else ""

    def _resolve_pattern(self, pattern: str) -> str:
        """解析单个文件路径模式，返回格式化的文件内容块。

        无通配符时直接读取单个文件；
        有通配符时展开为多个文件，按文件名排序。
        """
        path = self.workspace / pattern

        # 无通配符：直接读取文件
        if "*" not in pattern:
            return _file_block(path, f"/{pattern}")

        # 有通配符：展开为多个文件
        parent = path.parent
        glob_name = path.name
        if not parent.is_dir():
            return ""
        sections: list[str] = []
        for child in sorted(parent.glob(glob_name)):
            if child.is_file() and child.suffix == ".md":
                # 使用正斜杠，确保跨平台显示路径一致
                rel = child.relative_to(self.workspace).as_posix()
                block = _file_block(child, f"/{rel}")
                if block:
                    sections.append(block)
        return "\n\n".join(sections)


# ======================================================================
# 模块级工具函数（私有，无状态）
# ======================================================================


def _find_context_index(messages: list[AnyMessage], context_prefix: str) -> int | None:
    """找到上下文 HumanMessage 在消息列表中的索引。

    通过 context_label 前缀匹配，确保只匹配本中间件注入的消息。
    """
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) and isinstance(msg.content, str) and msg.content.startswith(context_prefix):
            return i
    return None


def _file_block(path: Path, display_path: str) -> str:
    """读取单个文件并包装为带标签的 markdown 块。"""
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return f"文件：{display_path}\n```markdown\n{content}\n```"


def _extract_task(messages: list[AnyMessage]) -> str:
    """从消息列表中提取最后一条 HumanMessage 的文本内容作为任务指令。"""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text = "\n".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ).strip()
                if text:
                    return text
    raise ValueError(
        "ContextAssemblerMiddleware: no HumanMessage with text found in request messages."
    )
