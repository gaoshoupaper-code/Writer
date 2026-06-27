"""adapt 包 —— AEGIS 进化循环（Phase 8，§4 Harness Adaptation）。

在 compose 之上建 Planner→Evolver→Critic+Gate 三阶段进化循环（决策 A6b）。
用 LangGraph StateGraph 实现（决策 A9），evolution 端编排（决策 A8a）。

模块：
  batch.py     固定测试集管理（A2a）
  verifier.py  多次打分均值（A3b）
  state.py     AdaptState TypedDict + Candidate 定义（E1a）
  graph.py     StateGraph 骨架（节点+边+条件边+loop）
  nodes/       各阶段节点实现（planner/evolver/critic/gate/runner/ship）
  api.py       /api/adapt/start 手动触发端点（A12a）

设计依据：设计文档 adapt 部分 E1-E8 + 需求 A1-A12。
"""
