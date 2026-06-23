"""platform.state —— 状态层（PR-13 从 core/ 迁入）。

工作区/线程元数据 + 产物存储 + 风格存储：
- thread_store: ThreadStore（元数据 CRUD）+ artifacts（WritingArtifactStore）
- artifact_store: WritingArtifactStore（写作产物读写）
- style_store: StyleStore（风格管理）
"""
