"""进化驱动器的 Skills 池（可复用片段）。

组织约定：每个 skill 一个子目录。
  skills/<skill_name>/
    SKILL.md    描述 + prompt 正文（何时触发、做什么、约束）
    tools.py    该 skill 的专属工具（如有）

加载机制：进化端自建轻量版，由 Agent 构建时按需挂载。
当前为空壳占位——具体 skill 后续按此约定填充。
"""
