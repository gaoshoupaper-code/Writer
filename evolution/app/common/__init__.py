"""common 包 —— 跨模块公共设施（evolve / eval_agent / tests 共用）。

设计依据：.claude/md/20260701_213000_进化端重构_设计.md（决策 D4 / S1）。

本包只收「被多个业务包平等依赖、不属于任何单一业务 Agent」的公共设施。
依赖方向：evolve / eval_agent / tests → core + common，业务包之间互不依赖。

模块：
  events.py        SSE 事件总线（进程内 session→事件队列映射，evolve + eval_agent 共用）
  flow_metrics.py  流程硬指标算子（协作拓扑/错误保障/资源消耗，eval_agent 用）
  evalset.py       评估集加载（测试用例 demand.md 加载，evolve + tests 共用）
  model_factory.py Agent 模型工厂（build_agent_model，eval_agent + evolve 共用）
  middleware/      跨 Agent 共用的 DeepAgent 中间件（NoFilesystemToolsMiddleware 等）

不放这里的东西：
  - HarnessConfig 配置对象 → harness_config/（业务语义强）
  - 评估打分（rubric/scoring）→ eval_agent/（评估 Agent 内部能力）
  - 基础设施（db/llm/settings/git_ops）→ core/
"""
