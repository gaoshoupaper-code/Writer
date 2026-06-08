---
type: require
status: draft
created: 2026-06-08 14:30
source: 图检测与执行追踪双向跳转 + 模型可读执行记录 + evaluation嵌套层级
related: []
---

# 检测系统增强：双向跳转、执行记录导出、evaluation 层级嵌套

## 核心诉求

在图检测中发现了异常数据点后，能直接定位到执行追踪中的对应节点；反之亦然。同时将执行链路持久化为模型可读格式，便于后续用 LLM 分析问题。此外，evaluation（评估代理）在执行追踪中的层级应嵌套在对应 subagent 内部，而非与其同级。

## 需求 1：图检测 ↔ 执行追踪双向跳转

### 当前状态
- 图检测（TokenChartPanel）和执行追踪（TraceChainTimeline）是 TracePanel 内的两个独立 tab
- 共享同一份 `detail.nodes` 数据源，但无交互关联
- 图检测数据点有 `agentName`、`loopIndex`、`inputTokens` 等信息，但**没有 `node_id` 关联**
- 执行追踪节点有 `node_id`，但切换 tab 后上下文丢失

### 已确认决策
- **触发方式**：点击数据点直接跳转（不用 hover 按钮）
- **视觉反馈**：自动滚动到目标位置 + 背景色/边框高亮，持续 2-3 秒后淡出
- **图 → 追踪**：点击图检测中的数据点 → 切换到"执行追踪" tab → 定位并高亮对应 LLM 节点
- **追踪 → 图**：在执行追踪中点击 LLM 节点 → 切换到"图检测" tab → 定位并高亮对应数据点

### 边缘场景
- **降采样点**：图检测视口外数据会降采样（桶采样保留 max/min），点击降采样点应定位到该桶中对应的实际 LLM 节点
- **非 LLM 节点**：执行追踪中 tool/todo/error 节点无法跳转到图检测（图只有 LLM 数据点），不触发跳转

## 需求 2：模型可读的执行记录

### 当前状态
- Trace JSONL 包含完整信息：完整 LLM 输入/输出、token 用量、耗时、工具调用及输出、错误详情
- 但体量大（单 trace 数百 KB~MB），直接喂模型 token 成本过高
- `TraceProjector` 将事件流投影为结构化的 `TraceDetail`，但不持久化

### 已确认决策
- **生成时机**：每次 trace 结束后自动导出（包括失败的 trace）
- **文件格式**：JSON
- **内容粒度**：摘要级别（agent 名称、操作类型、耗时、token 用量、状态、关键 I/O 摘要）
- **索引机制**：摘要中保留 node_id / event_id，据此回查原始 trace JSONL 文件获取完整数据
- **存放位置**：与原始 trace JSONL 同目录（如 `traces/20260608-1430/trace_xxx_summary.json`）

### 约束
- 摘要文件应尽量小（每个节点几十 token），以便模型高效阅读
- node_id / event_id 索引必须与原始 JSONL 中的 ID 一致

## 需求 3：evaluation 嵌套层级

### 当前状态
- `_build_compiled_pipeline_subagent` 管道中，evaluation 和 primary agent 同级
- `TraceProjector._agent_role()`：`*-subagent` → depth=1，所有 subagent 的 `parent_node_id="run"`
- 执行追踪中 evaluation 和 outline/writing 等同层显示

### 已确认决策
- **实现方式**：仅改 trace 投影（显示层），不改实际执行架构
- **识别方式**：名称规则匹配（agent_name 包含 "evaluation" 关键字）
- evaluation 在投影层 `parent_node_id` 指向 primary subagent 的 agent 节点，`depth=2`

### 已验证的 agent 名称格式
- Trace 事件中的 evaluation agent_name 模式：`"evaluation-subagent"`（outline管道）、`"detail-outline-evaluation-subagent"`（细纲管道）、`"writing-evaluation-subagent"`（写作管道）
- 识别规则：`"evaluation" in agent_name` 即可命中所有 evaluation agent
- 对应的 primary subagent 名称：`"outline-subagent"`, `"detail-outline-subagent"`, `"writing-subagent"`

---

## 关注面覆盖全景

| 关注面 | 状态 | 说明 |
|---|---|---|
| 功能范围 | ✅ 已覆盖 | 双向跳转 + 记录导出 + evaluation 嵌套 |
| 交互方式 | ✅ 已覆盖 | 点击跳转、自动滚动 + 限时高亮 |
| 数据格式 | ✅ 已覆盖 | JSON 摘要 + JSONL 完整数据 |
| 存储位置 | ✅ 已覆盖 | 与原始 trace 同目录 |
| evaluation 识别 | ✅ 已覆盖 | 名称规则匹配（含 "evaluation"） |
| 实现层级 | ✅ 已覆盖 | 仅投影层改动，不改执行架构 |
| 边缘场景 | ✅ 已覆盖 | 降采样点、非 LLM 节点、失败 trace |
