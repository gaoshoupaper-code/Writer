"""domains.writing.expert_agent 共享工具函数。

框架级类型（SubAgentSpec / MiddlewareFactory）已直接从 platform.agent.runtime import，
不再经此文件 re-export。本文件只保留写作专属的 apply_style_suffix。
"""

from __future__ import annotations


def apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。

    风格注入遵循 DeepAgent 的 SUFFIX 槽位语义：
    系统提示词（USER）在前，风格指导（SUFFIX）在后。
    风格文本紧贴对话历史，模型遵从度最高。
    """
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"


__all__ = ["apply_style_suffix"]
