"""platform.agent 子包：领域无关的 agent 编排骨架。

包含：
- ``hitl``：HITL interrupt payload 统一协议（DD4）
- ``base_service``：BaseAgentService 模板方法（DD7c，Phase 1 填充）
- ``middleware``：领域无关中间件（DD5，Phase 1/2 迁移）
- ``model_factory``：聊天模型构建（从 build_writer_model 抽出）
- ``skill_loader``：SKILL.md 加载（从 _compose_skills_backend 抽出）
"""
