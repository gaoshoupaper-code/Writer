"""Promote 闸门模块（数据闭环设计 Phase B）。

生产 trace → 数据集的清洗 + 标注流水线：
  filter.py     — 规则过滤（空/超长/格式/PII/越界），淘汰明显垃圾
  judge.py      — 调 eval_agent/scoring 打分，写 promote_tasks.judge_scores/verdict
  repo.py       — promote_tasks 表 CRUD + 状态机
  scheduler.py  — judge_scheduler 后台定时扫描（未 judge 的生产 trace）
  promote.py    — 标注 accept 时入库 growing（规范化 demand.md + reference.md）
  api.py        — 标注队列 UI API（列表/详情/决策）

状态机：pending → judging → needs_confirm → annotated → promoted
                                       └→ rejected
"""
