"""expert_agent 共享类型与工具函数。

SubAgentSpec / MiddlewareFactory 已迁入 platform.agent.runtime.types（PR-08，
框架级类型）。本文件 re-export 供 writer 内部过渡期引用，PR-11 writer 降级时清理。
apply_style_suffix 是写作专属工具，留在此处。
"""

from __future__ import annotations

# 框架级类型：从 runtime re-export（transitional，PR-11 清理）
from app.platform.agent.runtime import MiddlewareFactory, SubAgentSpec


# ======================================================================
# 写作专属工具函数
# ======================================================================


def apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。

    风格注入遵循 DeepAgent 的 SUFFIX 槽位语义：
    系统提示词（USER）在前，风格指导（SUFFIX）在后。
    风格文本紧贴对话历史，模型遵从度最高。
    """
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"


__all__ = [
    "MiddlewareFactory",
    "SubAgentSpec",
    "apply_style_suffix",
]
