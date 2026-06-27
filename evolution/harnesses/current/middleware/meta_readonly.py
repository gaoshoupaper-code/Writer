"""MetaReadOnlyMiddleware — Meta Agent 专属只读守卫中间件。

职责：
  Meta Agent（Director）只做宏观调度，严禁亲自写文件。
  当 Meta Agent 调用 write_file / edit_file 时，拦截该调用，不执行写入，
  并返回一条引导性提示，指明应由哪个专业子代理（interview / storybuilding /
  detail-outline / writing）来完成该类创作，促使 Meta 改走 task 工具委托子代理。

拦截规则：
  1. 仅拦截文件写入工具（write_file / edit_file），其他工具一律放行
  2. 写入工具一律拒绝，不调用 handler（Meta 无任何写权限，无需尝试）
  3. 根据被写入路径的前缀归类到对应子代理，返回引导性 ToolMessage
  4. 路径无法归类时返回通用引导（列出全部子代理）

注意：
  本中间件只装在 Meta Agent 层级。子代理（interview/storybuilding/
  detail-outline/writing）仍使用 FilesystemPathGuardMiddleware 做写路径
  安全校验——子代理需要写文件，不能加此只读守卫。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

# 需要拦截的文件系统写入工具名称
_FILESYSTEM_WRITE_TOOLS = {"write_file", "edit_file"}

# 路径前缀 → 子代理建议（按顺序匹配，命中即停）。
# 依据 FilesystemPathGuardMiddleware 写白名单 + system.md 子代理调用流程。
# 前缀用虚拟路径形式（带前导斜杠），匹配时先规范化输入路径再比对。
_PATH_TO_SUBAGENT: list[tuple[str, str]] = [
    ("/demand.md", "interview（需求分析）"),
    # storybuilding：三层故事构建产物（人物/故事线/世界观/总纲/卷纲）
    ("/character/", "storybuilding（故事构建）"),
    ("/outline.md", "storybuilding（故事构建）"),
    ("/storyline", "storybuilding（故事构建）"),
    ("/worldview.md", "storybuilding（故事构建）"),
    # review/ 下审查产物按子代理精确归类（精确路径优先于 /review/ 兜底）
    ("/review/storybuilding.md", "storybuilding（故事构建）"),
    ("/review/detail.md", "detail-outline（细纲）"),
    ("/review/chapter/", "writing（正文写作）"),
    ("/detail/", "detail-outline（细纲）"),
    # writing：正文及配套产物
    ("/chapter/", "writing（正文写作）"),
    ("/novel.md", "writing（正文写作）"),
    ("/state_log.md", "writing（正文写作）"),
    ("/review/", "writing（正文写作）"),
]

# 完整路径→子代理对照表，注入提示时一并给出，便于 Meta 自行判断
_FULL_MAPPING_TABLE = """\
  - /demand.md                          → interview（需求分析）
  - /character/*.md                     → storybuilding（故事构建）
  - /outline.md                         → storybuilding（故事构建）
  - /storyline.md、/storyline/*.md      → storybuilding（故事构建）
  - /worldview.md                       → storybuilding（故事构建）
  - /review/storybuilding.md            → storybuilding（故事构建）
  - /review/detail.md                   → detail-outline（细纲）
  - /review/chapter-*.md                → writing（正文写作）
  - /detail/*.md                        → detail-outline（细纲）
  - /chapter/*.md、/novel.md            → writing（正文写作）
  - /state_log.md                       → writing（正文写作）"""


class MetaReadOnlyMiddleware(AgentMiddleware):
    """Meta Agent 专属只读守卫。

    拦截 write_file / edit_file 工具调用：一律拒绝并返回 subagent 引导提示；
    其他工具调用放行。
    """

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：写入工具 → 引导提示；否则放行。"""
        blocked = self._block_if_write(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """拦截异步工具调用：写入工具 → 引导提示；否则放行。"""
        blocked = self._block_if_write(request)
        if blocked is not None:
            return blocked
        return await handler(request)

    def _block_if_write(self, request: Any) -> ToolMessage | None:
        """若是写入工具调用，返回引导性 ToolMessage；否则返回 None 放行。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        if tool_name not in _FILESYSTEM_WRITE_TOOLS:
            return None

        tool_call_id = _mapping_value(tool_call, "id")
        args = _mapping_value(tool_call, "args")
        raw_path = ""
        if isinstance(args, dict):
            raw_path = str(args.get("file_path") or "")
        subagent = _resolve_subagent(raw_path)
        return _tool_error(tool_name, tool_call_id, raw_path, subagent)


# ======================================================================
# 路径归类 / 引导提示构造
# ======================================================================


def _resolve_subagent(raw_path: str) -> str:
    """根据写入路径前缀归类到对应子代理；无法归类返回空串（走通用引导）。"""
    if not raw_path:
        return ""
    # 统一为虚拟路径前缀格式：带前导斜杠、正斜杠
    normalized = "/" + raw_path.strip().replace("\\", "/").lstrip("/")
    for prefix, subagent in _PATH_TO_SUBAGENT:
        if normalized == prefix or normalized.startswith(prefix):
            return subagent
    return ""


def _tool_error(
    tool_name: object,
    tool_call_id: object,
    raw_path: str,
    subagent: str,
) -> ToolMessage:
    """构造引导性工具消息，替代实际写入工具调用的返回值。"""
    target = f"目标路径：{raw_path}\n\n" if raw_path else "（未提供目标路径）\n\n"
    if subagent:
        directive = (
            f"本次目标路径应委托 {subagent} 子代理。\n"
            "请改用 task 工具委托该子代理完成，任务描述中写明用户需求与产物要求，"
            "不要重试 write_file/edit_file。"
        )
    else:
        directive = (
            "无法根据路径自动归类，请参照下方对照表选择对应子代理，"
            "改用 task 工具委托其完成，不要重试 write_file/edit_file。"
        )
    content = (
        "[只读守卫] Meta Agent（Director）只做宏观调度，禁止亲自写文件；"
        "所有创作产物必须由专业子代理落盘。\n\n"
        f"{target}"
        f"{directive}\n\n"
        "路径→子代理对照表：\n"
        f"{_FULL_MAPPING_TABLE}"
    )
    return ToolMessage(
        content=content,
        name=str(tool_name or "filesystem"),
        tool_call_id=str(tool_call_id or ""),
        status="error",
    )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)
