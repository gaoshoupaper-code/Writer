"""反思库模块（数据闭环设计 Phase D）。

失败 trace → 自动归纳失败模式 → 进化 Agent 检索参考。

  repo.py       — reflection_library 表 CRUD
  extractor.py  — 评估 badcase → 归纳失败模式 → 写表（hook 到 scoring）
  api.py        — 查询反思（可选，进化 Agent 内部直接调 repo）
"""
