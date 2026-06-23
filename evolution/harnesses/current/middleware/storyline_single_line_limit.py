"""StorylineSingleLineLimitMiddleware — 单次单线生成硬上限中间件。

职责：
  拦截 storybuilding 子代理的 write_file 调用，约束单次 storybuilding 子代理调用
  最多新增 ``max_new_lines`` 条故事线（``storyline/*.md``）。超限则返回 ToolMessage
  硬拦截，引导代理停止新增、基于现有内容收尾。

设计依据（见 .claude/md/20260611_214939_storybuilding单线中间件设计.md）：
  - ``FilesystemBackend.write`` 只创建新文件，对已存在文件返回 error；
    ``edit`` 只能改已存在文件。故 write_file 到「不存在的 storyline 文件」= 新增。
  - 计数对象 = ``/storyline/S{XX}-*.md`` 新增文件数（非写调用次数）：写入前查物理磁盘
    ``.exists()`` 判定，已存在 = 非新增（放行），不存在 = 新增（计数，超限拦截）。
    只认 S{XX} 开头的故事线文件——同目录 timeline.md 等非故事线文件不计入。
  - 计数周期 = 每次 storybuilding 子代理调用（before_agent 在每次 task 调用开始时重置），与 ``RevisionLimitMiddleware`` 一致。

使用方式：
  在 ``build_storybuilding_deep_subagent`` 构建时注入 storybuilding 子代理的 middleware 链。
  ``max_new_lines``: 单次运行最大新增故事线数，默认 1。
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from .path_guard import normalize_workspace_write_path

# 故事线详情文件虚拟路径：/storyline/S{XX}-<名>.md。只认 S{XX} 开头的故事线文件——
# 同目录的 timeline.md（全局时间线表，并非故事线）因此不被计入（见需求基准 D3/D4）。
# 宁严勿松：任何 S\d{2} 开头的 .md 都计入，避免漏算而绕过单线上限。
_STORYLINE_FILE = re.compile(r"^/storyline/S\d{2}[^/]*\.md$")


class StorylineSingleLineLimitMiddleware(AgentMiddleware):
    """单次单线生成硬上限中间件。

    拦截 write_file 到 ``storyline/*.md`` 的「新增」（文件写入前不存在），
    实例生命周期内累计计数，超过 ``max_new_lines`` 返回 ToolMessage 阻止写入。
    edit_file 与非 storyline 路径一概放行。
    """

    def __init__(self, workspace_path: Path, *, max_new_lines: int = 1) -> None:
        """
        Args:
            workspace_path:  工作区根目录绝对路径，用于把虚拟路径映射到物理磁盘查存在性。
            max_new_lines:   单次运行（实例生命周期）最大新增故事线数，默认 1。
        """
        self.workspace_path = workspace_path.resolve()
        self.max_new_lines = max_new_lines
        self._new_line_count = 0

    # ------------------------------------------------------------------
    # 调用周期重置（子代理每次被 task 调用开始时触发）
    # ------------------------------------------------------------------
    # 计数周期 = 「storybuilding 每被父 agent task 委托一次」。
    # 子代理 graph 一次编译、会话内多次复用同一实例，必须靠 before_agent 在每次
    # graph 执行开始时清零计数，否则额度会跨调用累积（见需求基准计数边界决策）。

    def before_agent(self, state: Any, runtime: Any) -> None:
        self._new_line_count = 0

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        self._new_line_count = 0

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._maybe_block(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        blocked = self._maybe_block(request)
        if blocked is not None:
            return blocked
        return await handler(request)

    # ------------------------------------------------------------------
    # 核心判定
    # ------------------------------------------------------------------

    def _maybe_block(self, request: Any) -> ToolMessage | None:
        """判定本次 write_file 是否构成「新增故事线」且超限。

        Returns:
            ``ToolMessage`` 表示超限拦截；``None`` 表示放行。

        步骤：
        1. 仅拦 write_file（edit_file 物理上无法新建文件，一概放行）。
        2. 自行 normalize 路径（解耦对 PathGuard 执行顺序的依赖）；非法路径放行交 PathGuard。
        3. 仅 storyline/*.md 受约束。
        4. 写入前查物理磁盘 .exists()：已存在 = 非新增（放行）；不存在 = 新增（计数，超限拦截）。
        """
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        if tool_name != "write_file":
            return None

        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None

        raw_path = args.get("file_path")
        try:
            normalized = normalize_workspace_write_path(raw_path, self.workspace_path)
        except ValueError:
            # 非法路径不归本中间件管，放行交 PathGuard 处理
            return None

        if not _STORYLINE_FILE.match(normalized):
            return None

        # 虚拟路径 → 物理路径查存在性（virtual_mode 文件真实落盘到 workspace_path）
        physical = self.workspace_path / normalized.lstrip("/")
        if physical.exists():
            # 已存在 = 非新增（write_file 自身会返回 already-exists error，无需重复拦截）
            return None

        self._new_line_count += 1
        if self._new_line_count <= self.max_new_lines:
            return None

        return self._limit_message(tool_call)

    def _limit_message(self, tool_call: Any) -> ToolMessage:
        """构造达上限的拦截消息：停止新增 + 指示子代理在返回摘要中转述（解读A 可见性）。"""
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=(
                f"已达单次单线生成上限（{self.max_new_lines} 条 / 本轮 storybuilding）。"
                "本次新增的故事线已写入，请停止创建更多故事线文件。"
                "请在返回给父代理的摘要中明确注明：「本轮因达到单线生成上限，已跳过后续新增」，"
                "再基于当前已有内容收尾返回。"
            ),
            name="write_file",
            tool_call_id=str(tool_call_id or ""),
        )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值（与其它中间件一致的取值方式）。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)
