---
type: design
status: draft
created: 2026-07-19 00:00
require: 20260719_evolution_agent_refactor.md
related:
  - evolution/app/evolve/agent/agent.py
  - evolution/app/evolve/agent/prompt.py
  - evolution/app/evolve/api.py
  - evolution/app/evolve/agent/middleware/flow_guard.py
  - evolution/desktop/src/pages/evolve.tsx
---

# 进化Agent重构 — 设计方案

## 0. 关联需求
见 `20260719_evolution_agent_refactor.md`（已 confirmed，27 个决策 A–AA）。

## 1. 现状核实（关键技术债）

### 1.1 最大架构债：`checkpointer=None`
- `agent.py:92-100` 构建时显式传 `checkpointer=None`
- 后果：DeepAgent 无状态，每次 `ainvoke` 后对话历史丢失
- 阻塞决策 B（双轨制需要持久化对话）和决策 D（拍板后落地需要保留 conversing 阶段的上下文）
- **必须优先解决**

### 1.2 已验证可行（零架构风险）
- DeepAgent `ainvoke` 已支持 messages 列表入参（`agent.py:152`）
- `@tool` 装饰器标准模式，所有 15 个工具一致（`tools/*.py`）
- FlowGuard 通过 `wrap_tool_call` 拦截工具调用（`flow_guard.py:82-95`），改造成阶段门控只需扩 `_guard_tool_call` 一处
- status 字段是自由 TEXT 无 CHECK 约束，加 conversing/finalizing **零 schema 改动**
- SSE 派生层 `_trace_event_to_sse`（`api.py:371-379`）只处理 `run_meta`，扩展空间大
- recorder callback 已自动拦 LLM/Tool 调用，工具调用事件零业务代码改造

### 1.3 已识别风险（迁入追踪）
| # | 风险 | 来源 | 严重度 |
|---|---|---|---|
| R1 | checkpointer=None → 多轮对话/HITL 落不了地 | 现状核实 | 🔴 阻塞 |
| R2 | SSE 无 Last-Event-ID，重连丢消息 | 现状核实 | 🟡 影响 |
| R3 | 多 worker 部署不安全（内存 task dict + asyncio.Queue） | 现状核实 | 🟢 当前单 worker，可延后 |
| R4 | conversing 不纳入单会话锁会导致并发进化冲突 | 决策 G 衍生 | 🟡 影响 |
| R5 | prompt.py 是硬编码 f-string，决策 Q 要拆静态/动态 | 决策 Q 衍生 | 🟡 影响 |
| R6 | conversing 跨多次 ainvoke，middleware 实例状态丢失 | 现状核实（flow_guard.py:45 _nudge_count） | 🟡 影响 |

## 2. 架构快照（待用户确认主路径后填充）

（下一轮填充）

## 3. § 实现风险清单（迁移自需求）

（需求文档无 § 实现风险清单段落 → 视为无显式风险，本设计文档 § 1.3 主动迁入追踪）

## 4. 数据与状态（待填充）

（下一轮填充）

## 5. 接口契约（待填充）

（下一轮填充）

## 6. 任务拆解 WBS（待填充）

（待填充）
