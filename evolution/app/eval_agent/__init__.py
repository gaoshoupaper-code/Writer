"""eval_agent 模块——独立评估 Agent（三功能解耦，决策 S1/T1-T11）。

评估从进化流水线抽离为独立顶层 Agent，并吸收了原 evaluation_engine 的
评估维度与打分逻辑（决策 S5：评估维度直接抽进 Agent）。

按要素分层结构：
  - agent.py              create_deep_agent 顶层 Agent 构建
  - prompt.py             评估 system prompt（只诊断不提方案，T4/S14）
  - ctx.py                EvaluationContext（评估专属 ctx + contextvar）
  - repo.py               evaluation_sessions 表 CRUD
  - api.py                评估 API（/api/eval-agent/*）
  - tools/                工具集（按数据源拆分）：
      trace.py            read_trace / read_trace_node / read_trace_range
      content.py          get_content_score + 后台内容评估任务
      report.py           write_eval_report
  - middleware/           评估 Agent middleware 池：
      no_fs.py            NoFilesystemToolsMiddleware（过滤框架自带 fs 工具）
  - scoring.py            双层打分（内容8维 + subagent4维，judge 调用编排）
  - eval_extractor.py     评估输入提取（按 subagent 从 trace 提取交付物）
  - rubrics/              评估维度 rubric（按品类，先聚焦玄幻/修仙）
  - skills/               可复用片段（空壳占位）

与 evolve/ 模块完全解耦：通过 DB（evaluation_sessions 表）交接评估报告，
不共享 context、不共享内存状态（S2）。

设计依据：.claude/md/20260702_131000_进化端Agent结构分层_设计.md（本次要素分层）
"""
