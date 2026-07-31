"""OverwriteRoutingMiddleware — 覆盖式产物写路由中间件（DEC-005 方案C / FR-002）。

职责：
  在 write_file 工具调用上识别"覆盖式产物"路径（review/*.md、evaluation.md 等），
  发出可观测信号，让覆盖写不再静默成功——既证明 OverwritingFilesystemBackend
  生效，也让 trace/干预流能审计"哪些覆盖发生了"。

设计要点（避免逻辑重复）：
  路径判定**复用** executor 隔离层的 OVERWRITE_PRODUCT_PATTERNS（方案C 唯一真相源），
  本中间件不自带第二份清单——这样 subclass 放宽/收紧清单时，中间件自动跟随，
  不会两处不同步。中间件只做"观测 + 信号"，不改写 tool_call、不绕过 handler：
  覆盖能力由 OverwritingFilesystemBackend 提供，本中间件不重复该能力。

  装配位置：在 FileWriteSerializeMiddleware 之外、ErrorRecovery 之内均可——
  它只读 request + 发信号，不影响串行化锁与重试链。建议放在 FileWriteSerialize
  之后、WriteResultInspector 之前（覆盖成功时 WriteResultInspector 不再抛错，
  本中间件的信号是覆盖发生的旁证）。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from app.platform.agent.runtime import OVERWRITE_PRODUCT_PATTERNS

logger = logging.getLogger(__name__)

# write_file 的路径参数名（兼容 file_path / path 两种命名）
_WRITE_FILE_TOOL = "write_file"
_PATH_FIELDS: tuple[str, ...] = ("file_path", "path")


def _is_overwrite_product(virtual_path: str) -> bool:
    """复用隔离层清单判断是否覆盖式产物路径（单一真相源，不重复定义）。"""
    return any(pattern.fullmatch(virtual_path) for pattern in OVERWRITE_PRODUCT_PATTERNS)


class OverwriteRoutingMiddleware(AgentMiddleware):
    """识别覆盖式产物 write_file 调用并发出可观测信号。

    覆盖能力本身由 OverwritingFilesystemBackend（隔离层 subclass）提供；本中间件
    只负责把"即将发生覆盖写"这一事实变成可审计的干预事件，便于线上观测覆盖行为
    是否如期发生（AC-003 的旁证）。
    """

    def __init__(
        self,
        *,
        intervention_callback: Callable[..., None] | None = None,
    ) -> None:
        self.intervention_callback = intervention_callback

    def _overwrite_path(self, request: Any) -> str | None:
        """若为 write_file 且命中覆盖式产物清单，返回路径；否则 None。"""
        tool_call = getattr(request, "tool_call", {})
        if _mapping_value(tool_call, "name") != _WRITE_FILE_TOOL:
            return None
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None
        for field in _PATH_FIELDS:
            path = args.get(field)
            if isinstance(path, str) and path and _is_overwrite_product(path):
                return path
        return None

    def _signal(self, path: str, hook: str) -> None:
        logger.info("OverwriteRouting: 覆盖式产物 write_file 命中 %s（%s）", path, hook)
        if self.intervention_callback is None:
            return
        try:
            self.intervention_callback(
                action="overwrite_routing",
                hook=hook,
                affected_fields=["file_path"],
                reason="overwrite_product_path",
            )
        except Exception:
            # 观测信号失败不影响业务写入
            pass

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        path = self._overwrite_path(request)
        if path is not None:
            self._signal(path, "wrap_tool_call")
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        path = self._overwrite_path(request)
        if path is not None:
            self._signal(path, "awrap_tool_call")
        return await handler(request)


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["OverwriteRoutingMiddleware"]
