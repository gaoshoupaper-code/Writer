"""platform.tools —— 跨域通用工具（PR-06 实体迁入）。

ask_user 是访谈式 HITL 的核心载体（writing/image 都用），领域无关，
从 writer/tools 物理迁入此目录。
"""

from app.platform.tools.ask_user import (
    ASK_USER_TOOL_DESCRIPTION,
    AskUserInput,
    AskUserOption,
    ask_user,
    build_ask_user_tool,
)

__all__ = [
    "ASK_USER_TOOL_DESCRIPTION",
    "AskUserInput",
    "AskUserOption",
    "ask_user",
    "build_ask_user_tool",
]
