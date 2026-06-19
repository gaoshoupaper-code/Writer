# ==============================================================================
# 专家代理模块（expert_agent）
#
# 本模块包含所有专业子代理，由主代理（meta_agent）按需委托。
#
# 目录结构（按关注点组织）：
#   expert_agent/
#   ├── types.py              — 共享类型（MiddlewareFactory, SubAgentSpec）
#   ├── factory.py            — DeepAgent 子代理工厂（build_deep_subagent）
#   ├── agents/               — 所有子代理构建函数
#   │   ├── storybuilding.py  — 增量式故事构建（人物+故事线+世界观+总纲+卷纲）
#   │   ├── detail_outline.py
#   │   └── writing.py
#   ├── evaluators/           — 所有评估器构建函数
#   │   ├── storybuilding.py  — 跨维度统一评估
#   │   ├── detail_outline.py
#   │   └── writing.py
#   ├── prompts/              — 所有系统提示词 + 评估提示词
#   │   ├── storybuilding_system.md
#   │   ├── storybuilding_evaluation.md
#   │   ├── character_system.md    （仅 CharacterService 使用）
#   │   ├── detail_outline_system.md
#   │   ├── detail_outline_evaluation.md
#   │   ├── writing_system.md
#   │   └── writing_evaluation.md
#   ├── skills/               — 所有 DeepAgent Skill（SOP 流程）
#   │   ├── storybuilding/SKILL.md
#   │   ├── detail-planning/SKILL.md
#   │   └── chapter-writing/SKILL.md
#   └── services/             — 面向 API 端点的服务层
#       └── character.py
# ==============================================================================
