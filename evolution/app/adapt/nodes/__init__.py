"""adapt 节点实现（Phase 8，Task 7.3-7.8）。

各阶段节点的实现，被 graph.py 组装成 StateGraph。
每个节点是 `def node(state: AdaptState) -> dict` 的函数，返回 state 更新。

节点列表：
  init          读 production config 作基准 + 读 batch
  run_baseline  跑基准 → baseline_traces + baseline_scores
  planner       查 DB 历史 + 读 baseline_traces → landscape
  evolver       读 landscape → K 候选 edits（含 manifest）
  run_candidates apply edits → HTTP /ab/run 轮询 → candidate_traces
  evaluate      多次打分（A3b）→ candidate_scores
  critic        读 scores+manifest → verdict: pass/reject/revision
  gate          候选 vs 基准对比（A7b）+ smoke test → ship/reject
  ship          存 config 快照 + git commit + push + reload executor
  loop_control  patience/budget 判断 → 继续/结束
"""
