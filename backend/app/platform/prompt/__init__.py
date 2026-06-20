"""platform.prompt —— 统一 prompt 加载子系统（Phase 5）。

从 monitoring（source of truth）按 name+label 加载 prompt，本地缓存，
monitoring 不可用时降级。
"""
from app.platform.prompt.loader import PromptContent, PromptLoader, get_loader, load_prompt

__all__ = ["PromptContent", "PromptLoader", "get_loader", "load_prompt"]
