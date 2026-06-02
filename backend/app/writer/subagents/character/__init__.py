# ==============================================================================
# Character 子代理模块
#
# 导出角色生成服务的公共 API。
#
# 公共 API：
#   CharacterService        — 角色生成服务类（面向上层 API）
#   build_character_subagent — 构建角色生成子代理规格
# ==============================================================================

from app.writer.subagents.character.character_subagent import (
    CharacterService,
    build_character_subagent,
)

__all__ = [
    "CharacterService",
    "build_character_subagent",
]
