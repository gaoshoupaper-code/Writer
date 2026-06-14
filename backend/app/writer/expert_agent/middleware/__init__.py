"""expert_agent 专属中间件包。

放置强业务耦合、仅服务特定子代理的约束中间件，就近与所属 agent 内聚：
  - StorylineSingleLineLimitMiddleware — storybuilding 单次单线生成硬上限
  - RevisionLimitMiddleware — 子代理 evolution 评估次数硬上限

与通用 ``app.writer.middleware``（path_guard / error_recovery / trace 等跨子代理
复用的中间件）分家。
"""
