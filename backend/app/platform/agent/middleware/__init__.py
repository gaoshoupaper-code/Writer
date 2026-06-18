"""platform.agent.middleware 子包：领域无关中间件（DD5）。

迁移自 app/writer/middleware（Phase 2 物理迁移）：
- TraceMiddleware / TraceCallbackHandler
- ErrorRecoveryMiddleware
- FilesystemPathGuardMiddleware（DD6a 白名单参数化）
- FileWriteSerializeMiddleware
- ContextAssemblerMiddleware
- ArtifactPrerequisiteMiddleware / ArtifactValidationMiddleware
- GoalMiddleware（DD6c，writing 用 image 不用）
- RevisionLimitMiddleware

写作专属中间件（MetaReadOnly / StorylineSingleLineLimit）不在此处，归 domains/writing。
"""
