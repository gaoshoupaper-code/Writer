"""evolve 包 —— 进化 Agent（plan→execute 两阶段驱动器 + 子代理）。

进化吃评估报告（来自 eval_agent / evaluation_sessions 表）产出代码改动，
落盘待审，由人工发版。与 eval_agent 完全解耦——通过 DB 交接，互不依赖。

结构（按要素分层）：
  顶层（API + 基础）：
    api.py            进化触发 + 查询 + SSE + 发版/丢弃
    ctx.py            EvolveContext（session 上下文 + contextvar）
    db.py             evolve_sessions 表 CRUD
    docs.py           design_doc / change_log 落盘读写
  driver/             驱动器（编排层）：
    agent.py          驱动器装配（DeepAgent + 2 子代理 + PhaseGuard）
    prompt.py         驱动器 system prompt
    middleware/
      phase_guard.py  PhaseGuardMiddleware（2 阶段白名单护栏）
  subagents/          子代理（方案 + 执行两阶段，各 prompt/tools/build 三件套）：
    plan/{prompt,tools,build}.py   方案子代理（读评估报告 + trace → design_doc.md）
    execute/{prompt,tools,build}.py 执行子代理（落地改动 + 校验 → change_log.md）
  skills/             可复用片段（空壳占位）

公共设施（events/flow_metrics/evalset/model_factory）已归入 common/。

设计依据：
  .claude/md/20260627_135113_进化端单Agent设计.md（原设计）
  .claude/md/20260701_213000_进化端重构_设计.md（上次重构）
  .claude/md/20260702_131000_进化端Agent结构分层_设计.md（本次要素分层）
"""
