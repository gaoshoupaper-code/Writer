"""expert_agent 专属中间件包。

与通用 ``app.writer.middleware``（path_guard / revision_limit / error_recovery 等
跨子代理复用的中间件）分家：此处放置仅服务特定子代理、强业务耦合的约束中间件，
就近与所属 agent 内聚。
"""
