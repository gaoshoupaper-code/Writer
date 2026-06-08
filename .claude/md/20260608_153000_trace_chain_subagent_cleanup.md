---
type: require
status: draft
created: 2026-06-08 15:30
source: 执行链路中 subagent 折叠条目后期消失 + task 工具噪声 + 上下文污染
related: []
---

# 执行链路 Trace 视图优化

## 核心诉求

每个子代理每次执行只展示一个可折叠的 subagent 条目，条目上显示该次执行的总耗时；
去掉 `task` 工具条目；完全过滤掉 meta-agent 的上下文段落（右侧抽屉）。

## 已确认的需求决策

| # | 决策点 | 用户选择 |
|---|--------|----------|
| D1 | Meta-agent 自身 LLM/Tool 节点 | **保留展示**（平铺在链路时间线上） |
| D2 | 后期 subagent 现象 | **完全没有折叠条目**（非"存在但为空"或"刷新后消失"） |
| D3 | Meta-agent 上下文段落 | **完全过滤掉**（右侧抽屉中不展示） |
| D4 | 每次调用的界定标准 | **按 task 工具的 tool_call_id 切分** |
| D5 | 同名多次调用是否显示序号 | **显示序号**（如 "writing #1"、"writing #2"） |
| D6 | evaluation/review 是否拆分 | **拆分**（每次调用独立折叠条目） |
| D7 | 耗时计算来源 | **事件时间戳计算**（首次事件 → 末次事件的差值） |
| D8 | Pipeline 内嵌套 | **保持嵌套**（review 嵌套在 writing 内部，与当前行为一致） |
| D9 | 无 task 工具的 subagent | **回退到当前行为**（按 agent_name 全局唯一，不拆分） |

## 根因分析

### RC1: 投影器为每个 agent_name 只创建一个 agent 节点

`projector.py:ensure_agent_node` 使用 `node_id = f"agent:{event.agent_name}"` 创建 agent 节点，
使用 `self.agent_nodes: set[str]` 去重。同一 agent_name 的事件全部挂在同一个 agent 节点下。

**后果**：writing-subagent 被调用 N 次（每章一次），但只有一个 `agent:writing-subagent` 节点，
出现在第一次调用的位置。后续调用的 LLM/Tool 节点虽然 parent_node_id 正确，
但在时间线上的折叠条目远离用户当前视口 → 等效于"没有折叠条目"。

### RC2: `task` 工具节点噪声

DeepAgent 框架的子代理委托通过 `tool_name="task"` 实现。
TraceMiddleware 拦截后产生 `depth=0` 的 tool 节点挂在 `agent:meta-agent` 下，
在链路上展示为独立的 tool 行，干扰阅读。

### RC3: agent 节点无 duration

`ensure_agent_node` 创建 agent 节点时只设置 `started_at`，
不设 `ended_at` 和 `duration_ms`。
subagent 的总耗时实际落在了 `task` 工具节点上。

### RC4: Meta-agent 上下文段落未被过滤

`_append_context` 为每个 LLM 输出 / Tool 输出创建上下文段落，
meta-agent 的段落（系统提示、规划思考、task 委托输入输出）与 subagent 的段落混在一起，
导致右侧抽屉被大量无关内容污染。

## 需求边界

### 做什么

- **[N1] 每次子代理调用产生独立的可折叠条目**：同一 agent_name 被调用多次时，
  每次调用产生独立的 agent 节点（如 `agent:writing-subagent:1`、`agent:writing-subagent:2`），
  条目出现在该次调用的第一个事件位置。
- **[N2] 去掉 task 工具节点**：`tool_name="task"` 的 tool 节点不再出现在链路时间线上。
- **[N3] Subagent 条目显示总耗时**：agent 节点携带 `duration_ms`，
  从该次调用的第一个事件到最后一个事件计算。
- **[N4] 过滤 meta-agent 上下文段落**：`agent_role="main"` 的上下文段落
  不出现在右侧抽屉的上下文面板中。
- **[N5] Meta-agent 节点保留展示**：meta-agent 自身的 LLM/Tool 节点（不含 task）
  继续在链路时间线上平铺展示。

### 不做什么

- 不改变 TraceMiddleware 的事件采集逻辑
- 不改变 meta-agent 的系统提示或执行流程
- 不修改前端 TraceChainTimeline 组件的折叠/展开交互
- 不处理流式 SSE 推送中的 task 工具事件（meta_agent.py 中的 SSE 逻辑保持现状）

## 待澄清

- [ ] N1 中 task tool_call_id 的获取方式：tool_start 事件是否携带 tool_call_id？
    → 已确认：TraceMiddleware._record_tool_start 从 request.tool_call.id 提取 tool_call_id ✓
- [ ] Pipeline 子代理内部多 agent（primary + evaluation）的事件归属判定
- [ ] Evaluation agent 嵌套父节点的确定：从 agent:writing-subagent:1 升级到 agent:writing-subagent:1

## 影响范围

### 后端（projector.py）

| 变更点 | 当前行为 | 目标行为 |
|--------|----------|----------|
| `ensure_agent_node` | 按 agent_name 全局唯一 | 按 task 调用局部唯一（agent:writing-subagent:1, :2） |
| task 工具事件 | 创建 tool 节点 | 吞掉（不创建节点），仅用作调用边界标记 |
| agent 节点 duration | 无 | 从首次事件 timestamp 到末次事件 timestamp |
| 上下文段落 | 全部保留 | 过滤 agent_role="main" 的段落 |

### 前端

| 变更点 | 当前行为 | 目标行为 |
|--------|----------|----------|
| nodeBodyLabel(agent) | 显示 agent_name | 显示 agent_name + 序号（如 "writing #1"） |
| 上下文面板 | 显示全部段落 | 过滤 agent_role="main" 的段落 |

## 收尾自检 — 关注面覆盖全景

| # | 关注面 | 状态 | 说明 |
|---|--------|------|------|
| 1 | 功能范围 | ✅ 已覆盖 | N1-N5 五项需求，做什么/不做什么已界定 |
| 2 | 数据模型 | ✅ 已覆盖 | node_id 格式变更、duration 计算、invocation 序号 |
| 3 | 边界场景 | ✅ 已覆盖 | pipeline 嵌套(D8)、无 task 工具回退(D9) |
| 4 | 过滤策略 | ✅ 已覆盖 | task 节点过滤(N2)、meta 上下文过滤(N4) |
| 5 | 向后兼容 | ✅ 已覆盖 | projector 是纯函数，重跑即可；前端无需结构变更 |
| 6 | 前端适配 | ✅ 已覆盖 | 仅 nodeBodyLabel 需改；折叠/展开逻辑自动适配新 node_id |
| 7 | 性能影响 | ✅ 不适用 | 20 章小说 → 20 个 agent 节点（原 1 个），增量可忽略 |
| 8 | 流式场景 | ✅ 不适用 | projector 运行于已存储事件，不涉及流式推送 |
| 9 | 异常处理 | ✅ 已覆盖 | task_start 无匹配 task_end 时 → 回退到当前行为(D9 策略) |
| 10 | 并发 task | ✅ 已覆盖 | 并行组逻辑(pg_id)不受影响；task 调用按 tool_call_id 隔离 |
