"""evolve 包 —— 单体进化 Agent（Writer 项目的 Agent 架构进化专家）。

进化吃评估报告（来自 eval_agent / evaluation_sessions 表）产出代码改动，
落盘待审，由人工发版。与 eval_agent 完全解耦——通过 DB 交接，互不依赖。

结构（单体 Agent + 基础设施）：
  顶层（API + 基础）：
    api.py            进化触发 + 查询 + SSE + 发版/丢弃
    ctx.py            EvolveContext（session 上下文 + contextvar）
    db.py             evolve_sessions 表 CRUD
    docs.py           design_doc / change_log 落盘读写
  agent/              单体进化 Agent：
    agent.py          build_evolve_agent() 单体装配（15 工具 + NoFS + FlowGuard）
    prompt.py         7 段全景 system prompt（要素认知 + 能力边界 + 运转机理）
    tools/            15 工具集（inspect 4 + writers 6 + flow 5）
    middleware/
      flow_guard.py   FlowGuardMiddleware（产出依赖约束：design_doc 先于 change_log）
  skills/             可复用片段（空壳占位）

公共设施（events/flow_metrics/evalset/model_factory/no_fs）已归入 common/。

设计依据：
  .claude/md/20260712_153000_重构进化端两个Agent.md（需求基准）
  .claude/md/20260712_160000_重构进化端两个Agent设计.md（本次单体化重构设计）
"""
