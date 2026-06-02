"""ContextAssemblerMiddleware — 通用上下文组装中间件。

职责：
  由主代理配置文件路径列表，在新阶段开始时从文件系统读取指定文件，
  组装为结构化上下文并注入模型请求。

  合并了原 StageControlMiddleware（阶段检测）和 ContextAssemblerMiddleware
  （文件读取 + 上下文注入）的职责，消除了子代理中的重复实现。

阶段检测机制（原 StageControlMiddleware 职责）：
  - 消息包含 ToolMessage → 工具调用循环中 → 透传
  - 消息不含 ToolMessage → 新阶段 → 读取文件并注入上下文

文件路径配置：
  由主代理在构建子代理时通过 file_paths 参数指定。
  支持通配符模式（如 "character/*.md"、"detail/chapter-*.md"）。
  文件按列表顺序读取，确保 prompt caching 前缀稳定。

消息替换策略：
  新阶段时丢弃旧消息，用 [上下文, 任务] 重新开始。
  这是有意为之：保持上下文窗口可控，避免过期信息残留。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

# 文件路径配置：静态列表或根据任务文本动态计算的回调函数
FilePaths = list[str] | Callable[[str], list[str]]


class ContextAssemblerMiddleware(AgentMiddleware):
    """通用上下文组装中间件。

    由主代理配置文件路径，在新阶段时读取文件并注入上下文。
    工具调用循环中直接透传，不做任何修改。

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

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        """新阶段时读取文件并注入上下文，工具循环中直接透传。"""
        if _has_tool_messages(request.messages):
            return handler(request)
        task = _extract_task(request.messages)
        context = self._build(task)
        return handler(request.override(messages=[
            HumanMessage(content=context),
            HumanMessage(content=task),
        ]))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        """异步版本。逻辑与同步版本完全相同。"""
        if _has_tool_messages(request.messages):
            return await handler(request)
        task = _extract_task(request.messages)
        context = self._build(task)
        return await handler(request.override(messages=[
            HumanMessage(content=context),
            HumanMessage(content=task),
        ]))

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


def _has_tool_messages(messages: list[AnyMessage]) -> bool:
    """检测消息列表中是否包含 ToolMessage。

    包含 ToolMessage 表示模型正在工具调用循环中。
    """
    return any(isinstance(m, ToolMessage) for m in messages)


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
