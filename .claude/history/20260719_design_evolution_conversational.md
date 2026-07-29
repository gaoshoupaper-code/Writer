---
type: design
status: draft
created: 2026-07-19 00:00
require: 20260719_evolution_agent_refactor.md
related:
  - evolution/app/evolve/agent/agent.py
  - evolution/app/evolve/agent/prompt.py
  - evolution/app/evolve/api.py
  - evolution/app/evolve/ctx.py
  - evolution/desktop/src/pages/evolve.tsx
---

# 进化Agent对话化重构 — 设计方案

## 0. 需求基准引用

关联需求文档：`20260719_evolution_agent_refactor.md`（status: confirmed，27 个决策 A–AA）

核心诉求：把进化Agent从"一次性批处理"重构为"有状态的长生命周期对话Agent"。

## 1. 全局定位

### 1.1 范式转变的本质

当前进化Agent是一个**批处理 Agent**：
- `run_evolve_session` 启动一个后台 asyncio task
- task 内 `agent.ainvoke(user_input)` 一次性跑完（探查→设计→落地→校验→产出）
- 用户只能通过 SSE 看日志，结束后看 review-report
- `checkpointer=None` → 无状态，跑完即销毁

要改成**长生命周期对话Agent**：
- Agent 在整个 session 期间持续存在（state 持久化在 checkpointer）
- 用户每发一条消息 → 触发一次 ainvoke → Agent 回应（可能调工具/可能纯文本）
- HITL interrupt 让 Agent 在关键点（如 propose 进化点后）交还控制权给用户
- 状态机推进：conversing → finalizing 由"用户拍板"这个外部事件触发

这是**范式转变**，不是加功能。涉及 4 层改造：
1. **Agent 层**：从一次性 → 可恢复的有状态 Agent
2. **API 层**：从"start + 看流"→ "start + 多轮 send_message + 阶段切换"
3. **数据层**：新增对话消息表 + 进化点表
4. **前端层**：从"日志流"→ "对话工作台 + 浮窗 + 双 Tab"

### 1.2 关键技术依赖（已确认）

| 能力 | DeepAgent 支持度 | 当前代码状态 |
|---|---|---|
| checkpointer 持久化 | ✅ 原生（传 SqliteSaver/PostgresSaver） | ❌ None |
| 多轮 ainvoke（thread_id 接续） | ✅ 原生 | ❌ 未启用 |
| HITL interrupt（interrupt_on + Command(resume)） | ✅ 原生（HumanInTheLoopMiddleware） | ❌ 未启用 |
| 流式（astream_events v2） | ✅ 原生 | ⚠️ 用的是 recorder 事件流，没用 astream_events |

**第一块多米诺骨牌**：`agent.py:99 checkpointer=None` 必须先改，否则多轮/HITL 都走不通。

### 1.3 架构方向（推荐主路径）

**"对话即 Agent 的主循环"**：
- Agent 不再"一次性跑完"，而是"等用户说话才动作"
- 用户消息驱动 Agent 推进，Agent 通过工具调用维护进化点状态
- "拍板"是一个 API 调用（`POST /evolve/sessions/{id}/finalize`），它做了两件事：
  1. 把进化点清单从 conversing 锁定为 finalizing
  2. 给 Agent 注入一条"开始落地"的系统消息，触发 ainvoke 进入落地阶段
- FlowGuard middleware 根据 `ctx.review_status` 决定是否拦截落地工具（决策 Z）

（具体方案细节随轮次填充）

---

## 2. 待填充段落（按轮次渐进）

- [ ] § 实现风险清单（迁移自需求） — 无（需求文档无此段落）
- [ ] 架构快照
- [ ] 数据与状态
- [ ] 接口契约
- [ ] 任务拆解 (WBS)
- [ ] § 术语表
