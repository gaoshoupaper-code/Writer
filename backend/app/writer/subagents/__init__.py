# ==============================================================================
# 子代理模块（subagents）
#
# 本模块包含所有专业子代理，由主代理（meta_agent）按需委托。
#
# 子代理一览：
#   outline/        — 大纲生成 + 评估管道（outline.md → evaluation.md）
#   writing/        — 正文写作 + 审查管道（chapter/*.md → review/*.md）
#   character/      — 角色生成服务（character/*.md）
#   detail_outline/ — 逐章细纲生成管道（detail/overview.md + detail/chapter-*.md）
#
# 每个子代理模块遵循统一的设计模式：
#   1. 子代理规格构建函数（build_*_subagent）
#   2. 管道构建函数（build_*_pipeline_subagent），复用 outline 的通用管道框架
#   3. 上下文组装、结果解析、修订指令等辅助函数
#
# 管道模式（Pipeline Pattern）：
#   主代理 → 子代理 → [primary agent → validate → secondary agent → validate]
#                    → [parse result → revise / advance / finish]
#                    → 输出汇总结果给主代理
# ==============================================================================
