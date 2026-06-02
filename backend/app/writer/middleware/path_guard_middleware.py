"""FilesystemPathGuardMiddleware — 文件系统路径安全守卫中间件。

职责：
  拦截代理对文件写入工具（write_file / edit_file）的调用，
  校验并规范化写入路径，防止越权或路径穿越攻击。

安全规则：
  1. 只允许写入预定义的工作区文件路径（正则白名单）
  2. 禁止路径穿越（../）和访问用户主目录（~/）
  3. 禁止网络路径（//）和 Windows 扩展路径前缀（//?/）
  4. Windows 绝对路径会自动转换为虚拟路径（需在工作区内）
  5. 运行时可通过 allowed_write_paths 参数动态扩展白名单

允许的写入路径白名单：
  - /character/<name>.md   — 角色档案文件
  - /outline.md            — 大纲文件
  - /evaluation.md         — 评估报告文件
  - /novel.md              — 小说正文文件
  - /chapter/<name>.md     — 正文章节文件
  - /review/<name>.md      — 审查报告文件
  - /detail/<name>.md      — 细纲文件
  - /state_log.md          — 状态日志文件

使用方式：
  在构建代理时传入 workspace_path（工作区根目录）和可选的额外白名单路径。
  中间件会自动拦截所有 write_file / edit_file 工具调用。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

# 需要拦截的文件系统写入工具名称
_FILESYSTEM_WRITE_TOOLS = {"write_file", "edit_file"}

# 允许的写入路径正则白名单（虚拟路径格式，如 /outline.md）
_ALLOWED_WRITE_PATHS = (
    re.compile(r"^/character/[^/]+\.md$"),     # 角色档案
    re.compile(r"^/outline\.md$"),              # 大纲
    re.compile(r"^/evaluation\.md$"),           # 评估报告
    re.compile(r"^/novel\.md$"),                # 小说正文
    re.compile(r"^/chapter/[^/]+\.md$"),        # 正文章节
    re.compile(r"^/review/[^/]+\.md$"),         # 审查报告
    re.compile(r"^/detail/[^/]+\.md$"),         # 细纲
    re.compile(r"^/state_log\.md$"),            # 状态日志
)

# Windows 绝对路径正则（如 C:/path/to/file）
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[a-zA-Z]:/")
# Windows 扩展路径前缀（\\?\）
_WINDOWS_EXTENDED_PREFIX = "//?/"


class FilesystemPathGuardMiddleware(AgentMiddleware):
    """文件系统路径安全守卫中间件。

    拦截写入工具调用，校验路径合法性，将合法路径规范化为虚拟路径格式。
    非法路径会被替换为 ToolMessage 错误响应，阻止实际写入操作。
    """

    def __init__(self, workspace_path: Path, allowed_write_paths: tuple[str, ...] = ()) -> None:
        """
        Args:
            workspace_path:      工作区根目录的绝对路径，用于解析 Windows 绝对路径
            allowed_write_paths: 额外允许的写入路径列表（运行时动态扩展白名单）
        """
        self.workspace_path = workspace_path.resolve()
        self.allowed_write_paths = set(allowed_write_paths)

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：校验路径 → 放行或返回错误。"""
        guarded = self._guard_request(request)
        if isinstance(guarded, ToolMessage):
            # 路径不合法，返回错误消息替代实际调用
            return guarded
        return handler(guarded)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """拦截异步工具调用：校验路径 → 放行或返回错误。"""
        guarded = self._guard_request(request)
        if isinstance(guarded, ToolMessage):
            return guarded
        return await handler(guarded)

    def _guard_request(self, request: Any) -> Any | ToolMessage:
        """核心校验逻辑：检查并规范化写入路径。

        步骤：
        1. 只拦截写入工具（write_file / edit_file），其他工具放行
        2. 从工具参数中提取 file_path
        3. 调用 normalize_workspace_write_path 校验并规范化路径
        4. 将规范化后的路径回写进工具参数
        5. 如果路径不合法，返回 ToolMessage 错误
        """
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        if tool_name not in _FILESYSTEM_WRITE_TOOLS:
            # 非写入工具，直接放行
            return request

        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return _tool_error(tool_name, _mapping_value(tool_call, "id"), "tool args must be an object")

        raw_path = args.get("file_path")
        try:
            # 校验并规范化路径
            normalized_path = normalize_workspace_write_path(
                raw_path,
                self.workspace_path,
                self.allowed_write_paths,
            )
        except ValueError as exc:
            # 路径不合法，返回错误消息
            return _tool_error(tool_name, _mapping_value(tool_call, "id"), str(exc))

        # 将规范化后的路径回写进工具参数（FilesystemBackend 会使用虚拟路径）
        modified_call = {
            **tool_call,
            "args": {
                **args,
                "file_path": normalized_path,
            },
        }
        return request.override(tool_call=modified_call)


# ======================================================================
# 路径规范化函数（公开，供测试使用）
# ======================================================================


def normalize_workspace_write_path(
    raw_path: object,
    workspace_path: Path,
    allowed_write_paths: set[str] | None = None,
) -> str:
    """校验并规范化工作区写入路径。

    将各种格式的输入路径转换为标准虚拟路径（如 /outline.md），
    同时执行安全校验。

    Args:
        raw_path:             原始路径（可以是字符串、虚拟路径或 Windows 绝对路径）
        workspace_path:       工作区根目录（用于解析 Windows 绝对路径）
        allowed_write_paths:  额外允许的写入路径集合

    Returns:
        规范化后的虚拟路径字符串

    Raises:
        ValueError: 路径不合法（穿越、越权、格式错误等）
    """
    if allowed_write_paths is None:
        allowed_write_paths = set()

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("file_path must be a non-empty string")

    # 统一使用正斜杠
    path = raw_path.strip().replace("\\", "/")

    # 剥离 Windows 扩展路径前缀
    if path.startswith(_WINDOWS_EXTENDED_PREFIX):
        path = path[len(_WINDOWS_EXTENDED_PREFIX) :]

    # 禁止网络路径或歧义路径
    if path.startswith("//"):
        raise ValueError(f"Refusing network or ambiguous path: {raw_path}")

    # 禁止用户主目录路径（防止路径穿越）
    if path.startswith("~"):
        raise ValueError(f"Path traversal not allowed: {raw_path}")

    # 处理 Windows 绝对路径：转换为相对于工作区的虚拟路径
    if _WINDOWS_ABSOLUTE_PATH.match(path):
        path = _virtual_path_from_workspace_absolute(path, workspace_path)
    elif not path.startswith("/"):
        # 相对路径：添加前导斜杠变为虚拟路径
        path = f"/{path}"

    # 规范化路径（去除多余的斜杠和 . 分段）
    normalized = PurePosixPath(path).as_posix()
    parts = PurePosixPath(normalized).parts

    # 禁止路径穿越（../）
    if ".." in parts:
        raise ValueError(f"Path traversal not allowed: {raw_path}")

    # 去除尾部斜杠
    if normalized != "/":
        normalized = normalized.rstrip("/")

    # 检查是否在额外白名单中（动态扩展路径）
    if normalized in allowed_write_paths:
        return normalized

    # 检查是否匹配预定义白名单
    if not any(pattern.fullmatch(normalized) for pattern in _ALLOWED_WRITE_PATHS):
        allowed = "/character/*.md, /outline.md, /evaluation.md, /novel.md, /chapter/*.md, /review/*.md, /detail/*.md, /state_log.md"
        if allowed_write_paths:
            allowed = f"{allowed}, {', '.join(sorted(allowed_write_paths))}"
        raise ValueError(
            "Write path is outside the allowed workspace files: "
            f"{normalized}. Allowed: {allowed}"
        )
    return normalized


def _virtual_path_from_workspace_absolute(raw_path: str, workspace_path: Path) -> str:
    """将 Windows 绝对路径转换为相对于工作区根目录的虚拟路径。

    例如：C:/workspace/thread-123/outline.md → /outline.md
    如果路径在工作区外，抛出 ValueError。
    """
    try:
        resolved_path = Path(raw_path).resolve()
        relative_path = resolved_path.relative_to(workspace_path.resolve())
    except ValueError:
        raise ValueError(f"Windows absolute path is outside the current workspace: {raw_path}") from None
    return f"/{relative_path.as_posix()}"


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


def _tool_error(tool_name: object, tool_call_id: object, message: str) -> ToolMessage:
    """构造工具错误消息，替代实际工具调用的返回值。"""
    return ToolMessage(
        content=f"Error: {message}",
        name=str(tool_name or "filesystem"),
        tool_call_id=str(tool_call_id or ""),
        status="error",
    )
