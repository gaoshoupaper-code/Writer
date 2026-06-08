---
type: design
status: draft
created: 2026-06-08 15:00
require: 20260608_143000_trace-chart-link-eval-nest.md
related: []
---

# 设计文档：双向跳转、执行记录导出、evaluation 嵌套

## 全局定位

三个需求互相独立，无耦合依赖，可并行实现：

| 需求 | 改动范围 | 性质 |
|---|---|---|
| 双向跳转 | 纯前端 `TracePanel` + `TokenChartPanel` + `TraceChainTimeline` | 交互层 |
| 执行记录导出 | 纯后端 `TraceRecorder` + 新增 summary 模块 | 数据层 |
| evaluation 嵌套 | 纯后端 `TraceProjector` 投影逻辑 | 投影层 |

---

## 需求 1：图检测 ↔ 执行追踪双向跳转

### 已确认决策

**D1-① DataPoint 携带 nodeId**
- `DataPoint` 类型新增 `nodeId: string` 字段
- `extractLoops()` 遍历 `detail.nodes` 时直接携带 `node.node_id`
- 降采样 `downsampleSeries()` 保留每个采样点的 `nodeId`
- 点击降采样点 → 跳转到 max/min 代表的那个 LLM 节点

**D1-② 跨 Tab 跳转：TracePanel 状态提升**
- `TracePanel` 新增 `highlightNodeId: string` 状态
- 新增 `requestHighlight(nodeId, source: "chart" | "trace")` 方法
  - 来源 "chart" → 切到 "trace" tab，传 `highlightNodeId` 给 `TraceChainTimeline`
  - 来源 "trace" → 切到 "chart" tab，传 `highlightNodeId` 给 `TokenChartPanel`
- `highlightNodeId` 经 props 下发给子组件，子组件负责滚动+高亮

**D1-③ 高亮动画：CSS @keyframes + scrollIntoView**
- 目标元素加 CSS class `trace-highlight-pulse`
- `@keyframes` 2.5s 背景色渐变淡出
- JS 侧 `scrollIntoView({ behavior: "smooth", block: "center" })`
- `onAnimationEnd` 回调移除 class

### 数据流（图 → 追踪）

```
TokenChartPanel onClick dataPoint
  → props.onNavigateToTrace(dataPoint.nodeId)
  → TracePanel.requestHighlight(nodeId, "chart")
    → setActiveTab("trace")
    → setHighlightNodeId(nodeId)
    → TraceChainTimeline 收到 highlightNodeId
      → scrollIntoView + CSS pulse 高亮
```

### 数据流（追踪 → 图）

```
TraceChainDrawer 内"在图检测中定位"按钮 onClick
  → props.onNavigateToChart(node.node_id)
  → TracePanel.requestHighlight(nodeId, "trace")
    → setActiveTab("chart")
    → setHighlightNodeId(nodeId)
    → TokenChartPanel 收到 highlightNodeId
      → 找到对应的 loopIndex
      → 滚动容器 scrollLeft 定位
      → SVG circle 加 CSS pulse 高亮
```

**D1-⑨ 追踪→图触发方式：抽屉内按钮**
- 单击 LLM 节点仍打开抽屉（现有行为不变）
- 在 `TraceChainDrawer` 底部新增"在图检测中定位"按钮
- 仅 LLM 节点的抽屉显示该按钮
- 点击按钮 → 关闭抽屉 + 切 tab + 高亮图数据点

### 边缘场景处理

- **非 LLM 节点**：抽屉中不显示"在图检测中定位"按钮（只有 LLM 节点在图中有对应数据点）
- **降采样点**：每个降采样点都携带 `nodeId`，直接跳到对应节点
- **高亮消散**：`highlightNodeId` 在动画结束后重置为 `""`（通过 `onAnimationEnd` 回调通知父组件）

---

## 需求 2：模型可读的执行记录

### 已确认决策

**D2-④ Summary 生成：复用投影结果**
- 在 `_finalize_run()` 末尾、`_cleanup_run_state()` 之前生成
- 调用 `_read_run_detail()` 获得完整 `TraceDetail`（含投影 nodes/context/todos）
- 从 `TraceDetail.nodes` 提取摘要字段，写入 JSON 文件
- 然后再执行 `_cleanup_run_state()` 清理内存

**D2-⑤ Schema 粒度：nodes 摘要 + 顶层统计**
- 只保留 nodes 摘要（不含完整 context/todos）
- 顶层附加 `context_count`、`todo_count` 统计字段
- 每节点 ~20-30 token，控制总体积

**D2-⑥ 文件命名：`{trace_id}_summary.json`**
- 与原始 JSONL 同目录：`traces/20260608-1430/trace-xxx_summary.json`
- 发现方式：index.json 的 `path` 字段 `.jsonl` → `_summary.json`
- 不改 index.json schema

### Summary JSON Schema

```json
{
  "trace_id": "trace-xxx",
  "status": "completed",
  "started_at": "2026-06-08T14:30:00Z",
  "ended_at": "2026-06-08T14:35:00Z",
  "duration_ms": 300000,
  "total_input_tokens": 50000,
  "total_output_tokens": 12000,
  "context_count": 42,
  "todo_count": 8,
  "nodes": [
    {
      "node_id": "llm:trace-xxx-3",
      "parent_node_id": "agent:outline-subagent",
      "kind": "llm",
      "agent_name": "outline-subagent",
      "depth": 1,
      "status": "completed",
      "model_name": "claude-sonnet-4-6",
      "duration_ms": 3200,
      "usage": { "input_tokens": 5000, "output_tokens": 1200 },
      "summary": "claude-sonnet-4-6: 生成大纲初稿…",
      "raw_event_ids": ["trace-xxx-3"]
    }
  ]
}
```

### 生成流程

```
_finalize_run(status, duration_ms, error)
  → _read_run_detail(thread, trace_id)   # 复用投影
  → build_trace_summary(TraceDetail)      # 提取摘要
  → write summary JSON                    # 写文件
  → _write_index(...)                     # 写 index
  → _cleanup_run_state(trace_id)          # 清理内存
```

## 需求 3：evaluation 嵌套层级

### 已确认决策

**D3-⑦ 识别规则：从 agent_name 字符串模式推导 primary**
- 识别 evaluation：`"evaluation" in agent_name`
- 推导 primary 名称：`agent_name.replace("-evaluation", "")`
  - `"evaluation-subagent"` → `"outline-subagent"` ✅
  - `"detail-outline-evaluation-subagent"` → `"detail-outline-subagent"` ✅
  - `"writing-evaluation-subagent"` → `"writing-subagent"` ✅
- 不改执行架构，纯投影层改动

**D3-⑧ 投影层改动 + 前端缩进适配**
- `_agent_depth()` 改动：evaluation agent → `depth=2`
- `ensure_agent_node()` 改动：evaluation agent 的 `parent_node_id` 指向 `"agent:{primary_name}"` 而非 `"run"`
- evaluation 下的 LLM/tool/error 子节点 `depth=3`（跟随 agent depth +1）
- 前端 `TraceChainTimeline` 缩进逻辑：`indent = node.depth > 0 ? 28 : 0` → `indent = node.depth * 28`

### 投影层改动清单

**`backend/app/writer/trace/projector.py`**：

1. 新增辅助函数：
```python
def _is_evaluation_agent(agent_name: str | None) -> bool:
    return agent_name is not None and "evaluation" in agent_name

def _evaluation_primary_name(agent_name: str) -> str:
    """evaluation-subagent → outline-subagent"""
    return agent_name.replace("-evaluation", "")
```

2. 改 `_agent_depth()`：
```python
def _agent_depth(agent_name: str | None) -> int:
    if _is_evaluation_agent(agent_name):
        return 2
    return 1 if _agent_role(agent_name) == "subagent" else 0
```

3. 改 `ensure_agent_node()` — evaluation agent 的 `parent_node_id`：
```python
if _is_evaluation_agent(event.agent_name):
    parent_node_id = f"agent:{_evaluation_primary_name(event.agent_name)}"
else:
    parent_node_id = "run"
```

4. evaluation 下的子节点（LLM/tool/error）的 `depth` 已通过 `_agent_depth(agent_name)` 计算——因为它们继承了 evaluation agent 的 `agent_name`，而 `_agent_depth` 现在对 evaluation 返回 2，这些子节点的 `depth` 自然就是 2。但它们的 `parent_node_id` 已经指向 evaluation agent 节点（`_agent_node_id(end)`），无需额外改动。

⚠️ **注意**：当前 LLM/tool/error 子节点的 `depth` 来自 `_agent_depth(agent_name)` 而不是 `agent_depth + 1`。这意味着 evaluation 下的子节点 depth=2（与 evaluation agent 同级）。需要额外处理：**evaluation 下的子节点 depth 应为 3**（agent depth=2, 子节点 depth=3）。

**修正方案**：新增 `_child_depth()` 函数，或在 `add_llm_node`/`add_tool_node` 等方法中，对 evaluation agent 的子节点 depth+1：
```python
def _agent_depth(agent_name: str | None) -> int:
    if _is_evaluation_agent(agent_name):
        return 2
    return 1 if _agent_role(agent_name) == "subagent" else 0

def _child_depth(agent_name: str | None) -> int:
    """evaluation 子节点 depth=3，其余跟随 agent_depth"""
    if _is_evaluation_agent(agent_name):
        return 3
    return _agent_depth(agent_name)
```

在 `add_llm_node`/`add_tool_node`/`add_llm_error_node`/`add_tool_error_node`/`add_running_llm_node`/`add_running_tool_node` 中，将 `depth=_agent_depth(...)` 改为 `depth=_child_depth(...)`。

**`frontend/components/workspace/TraceChainTimeline.tsx`**：

5. 缩进逻辑改动：
```tsx
// 前：const indent = node.depth > 0 ? 28 : 0;
// 后：
const indent = node.depth * 28;
```

6. 折叠逻辑适配：`agentChildCounts` 基于 `parent_node_id` 统计，evaluation 的子节点 `parent_node_id` 指向 evaluation agent 节点——**无需改动**，自动生效。

---

## 任务拆解 (WBS)

### Phase 1：后端改动（需求 2 + 需求 3，可并行）

**T1.1 Evaluation 投影嵌套**（需求 3）
- 文件：`backend/app/writer/trace/projector.py`
- 新增 `_is_evaluation_agent()`、`_evaluation_primary_name()`、`_child_depth()`
- 改 `_agent_depth()` → evaluation 返回 2
- 改 `ensure_agent_node()` → evaluation 的 parent 指向 primary agent
- 改 6 个 `add_*_node()` 方法中 `depth=` 使用 `_child_depth()`
- 验证：用现有 trace JSONL 回放，确认 evaluation 节点 depth=2、parent 正确

**T1.2 Summary 生成模块**（需求 2）
- 新建 `backend/app/writer/trace/summary.py`
- 实现 `build_trace_summary(detail: TraceDetail) -> dict` 按 D2-⑤ Schema
- 验证：单元测试，mock TraceDetail 输入，校验输出 JSON 结构

**T1.3 Summary 写入集成**
- 文件：`backend/app/writer/trace/recorder.py`
- 改 `_finalize_run()`：在 `_write_index` 之后、`_cleanup_run_state` 之前，调用 `build_trace_summary` + 写文件
- 验证：跑一次完整 trace，确认 summary JSON 生成

### Phase 2：前端改动（需求 1 + 需求 3 缩进）

**T2.1 DataPoint 携带 nodeId**
- 文件：`frontend/components/workspace/TokenChartPanel.tsx`
- `DataPoint` 类型加 `nodeId: string`
- `extractLoops()` 中携带 `node.node_id`
- 降采样 `downsampleSeries()` 透传 `nodeId`
- 验证：console.log 确认每个 DataPoint 都有 nodeId

**T2.2 TracePanel 跳转协调**
- 文件：`frontend/components/workspace/TracePanel.tsx`
- 新增 `highlightNodeId` 状态 + `requestHighlight()` 方法
- 给 `TokenChartPanel` 传 `onNavigateToTrace` + `highlightNodeId`
- 给 `TraceChainTimeline` 传 `onNavigateToChart` + `highlightNodeId`
- 验证：点击图数据点 → 自动切 tab → 跳到追踪节点

**T2.3 TraceChainTimeline 高亮接收**
- 文件：`frontend/components/workspace/TraceChainTimeline.tsx`
- 接收 `highlightNodeId` → `useEffect` 监听变化 → `scrollIntoView` + CSS pulse
- 传递 `onHighlightEnd` 回调通知父组件清除 highlightNodeId
- 验证：从图跳转过来 → 自动滚动到节点 + 高亮

**T2.3b TraceChainDrawer 跳转按钮**（D1-⑨）
- 文件：`frontend/components/workspace/TraceChainDrawer.tsx`
- LLM 节点抽屉底部新增"在图检测中定位"按钮
- 点击 → 调 `onNavigateToChart(node.node_id)` → 关闭抽屉 + 切 tab + 高亮
- 非 LLM 节点抽屉不显示该按钮
- 验证：打开 LLM 抽屉 → 点按钮 → 切到图检测 → 高亮对应数据点

**T2.4 TokenChartPanel 高亮 + 跳转触发**
- 文件：`frontend/components/workspace/TokenChartPanel.tsx`
- 数据点点击事件改为触发 `onNavigateToTrace`
- 接收 `highlightNodeId` → 找到对应 loopIndex → scrollLeft 定位 + SVG circle 高亮
- 验证：完整双向跳转

**T2.5 CSS 高亮动画**
- 文件：`frontend/components/workspace/TracePanel.css`（或对应样式文件）
- 新增 `.trace-highlight-pulse` class + `@keyframes` 2.5s 淡出
- 验证：高亮效果视觉确认

**T2.6 Evaluation 嵌套缩进**
- 文件：`frontend/components/workspace/TraceChainTimeline.tsx`
- `indent = node.depth * 28`
- 验证：evaluation 节点及其子节点比 primary subagent 多缩进一级

### Phase 3：集成验证

**T3.1 端到端验证**
- 跑一次完整写作任务
- 确认：summary JSON 生成、evaluation 嵌套显示正确、双向跳转工作正常
