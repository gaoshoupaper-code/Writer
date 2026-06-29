"""evolve 包 —— 进化端简化为单进化 Agent（替换 adapt 四阶段 graph）。

设计依据：.claude/md/20260627_135113_进化端单Agent设计.md

核心组件：
  agent.py    进化 Agent（DeepAgent，全流程一把手 + middleware 护栏）
  tools.py    领域工具集（run_baseline/run_candidate/read_trace/read_surface/read_verifier/report）
  guard.py    EvolutionGuardMiddleware（report 前必须有两次 verifier 分数）
  prompt.py   进化 Agent 的 system prompt
  api.py      POST /evolve/start + GET /sessions/{id}/stream（SSE）
  events.py   SSE 事件总线（Agent 工具调用 → 前端）
  evalset.py  评估集加载
  db.py       evolve_sessions 表 + CRUD
  verifier.py 从旧 adapt 迁移的多次打分（评估判据，复用）
"""
