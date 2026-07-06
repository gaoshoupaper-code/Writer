"""Benchmark 矩阵 + Runner 模块（数据闭环设计 Phase C）。

跨版本 harness 在同一 golden revision 上跑全 case → 评估 → leaderboard 对比。

  repo.py     — benchmark_runs 表 CRUD + 矩阵查询
  runner.py   — 后台异步执行（case × 版本 → 调 executor → 轮询 → 评估 → 写表）
  api.py      — 触发 run / 查 leaderboard / 查批次状态

触发点（决策 A9/D12）：
  1. 发版后 UI 手动触发"跑 benchmark"
  2. golden 升级后手动触发"重跑最近 K=3 版本"
"""
