---
type: design
status: draft
created: 2026-06-08 16:48
require: 20260608_153000_trace_chain_subagent_cleanup.md
related: []
---

# Trace 视图：Subagent 实例化拆分 + task 过滤 + meta 上下文过滤

## 架构快照

### 当前架构

**双投影器架构**（后端 + 前端各自独立实现投影逻辑）：

```
TraceMiddleware 采集事件 → thread_store 持久化
         ↓                              ↓
  SSE 流式推送 → 前端 trace.ts          API 请求 → 后端 projector.py
  (增量 appendLiveTraceEvent)           (全量 project())
         ↓                              ↓
         ┠→ TraceDetail { nodes, context, todos, events }
         ↓
  TraceChainTimeline.tsx 渲染 nodes
  TracePanel.tsx 渲染 context（右侧抽屉）
```

- **后端 projector** (`backend/app/writer/trace/projector.py`): Python，API `/trace/{trace_id}` 调用
- **前端 projector** (`frontend/lib/trace.ts`): TypeScript，SSE 流式事件增量构建
- 两者逻辑**必须同步**，否则流式视图和刷新后视图不一致

### 已确认的架构方向

1. **Subagent 实例化**：同一 agent_name 多次调用时，每次产生独立 agent 节点
   - node_id 格式：`agent:{agent_name}:{序号}`（如 `agent:writing-subagent:1`）
   - 按 `task` 工具的 `tool_call_id` 切分调用边界（D4）
   - 显示序号如 "writing #1"、"writing #2"（D5）
2. **task 工具拦截**：`tool_name="task"` 的事件不创建 tool 节点，仅用作调用边界标记
3. **evaluation 归属**：按 agent_name 归属——通过 `_evaluation_primary_agent_name()` 找到 primary 的 agent_name，再查表找到当前活跃实例
4. **meta-agent 上下文过滤**：后端 projector 照常生成所有 context；**前端 TracePanel 渲染时按选中节点过滤**——选中 subagent 节点时过滤 `agent_role="main"` 的段落，选中 meta-agent 节点时不过滤
5. **前端 projector 同步变更**：前端 `trace.ts` 镜像后端实例化拆分 + task 拦截变更（不含 N4 过滤）

### 技术断层追踪

| # | 断层 | 状态 | 说明 |
|---|------|------|------|
| F1 | task 事件拦截粒度 | ✅ 已共识 | 拦截事件、提取边界信息、不创建 TraceNode |
| F2 | 序号分配时序 | ✅ 已共识 | task_start 时即刻分配序号 |
| F3 | evaluation 嵌套归属 | ✅ 已确认 | 按 agent_name 归属，查 agent_invocation_counter |
| F4 | 前端 projector 同步 | ✅ 已确认 | 前后端同步变更 |
| F5 | N4 过滤策略 | ✅ 已修正 | **前端渲染时过滤**：后端照常生成所有 context；前端 TracePanel 按 `selectedNode.agent_role` 过滤 |
| F6 | 无 task 事件时的回退 | ✅ 已设计 | agent_invocation_counter 未设置 → 回退到 `agent:{name}` 全局唯一 |

---

## 数据与状态

### 节点 ID 变更

```
当前：agent:{agent_name}              → 全局唯一
目标：agent:{agent_name}:{invocation}  → 按调用实例唯一
回退：agent:{agent_name}              → 无 task 事件时保持原样（D9）
```

### 关键事件时序（已验证）

```
事件流（以 meta-agent 委托 writing-subagent 为例）：

1. llm_start  (agent_name="meta-agent")              → meta-agent 思考
2. llm_end    (agent_name="meta-agent", tool_calls=[{id:"tc-1", name:"task"}])
3. tool_start (agent_name="meta-agent", tool_name="task", tool_call_id="tc-1")
                                                      → ⚡ 拦截点：注册 task 边界
4. llm_start  (agent_name="writing-subagent")         → writing 实例 #1 开始
5. llm_end    (agent_name="writing-subagent")
6. llm_start  (agent_name="writing-evaluation-subagent") → evaluation 嵌套
7. llm_end    (agent_name="writing-evaluation-subagent")
8. tool_end   (agent_name="meta-agent", tool_name="task", tool_call_id="tc-1")
                                                      → ⚡ 拦截点：关闭 task 边界

→ 重复 3-8 为 writing #2, #3, ...
```

### _ProjectionState 新增字段

```python
# task 边界追踪
current_task_call_id: str | None = None
# agent_name → 该 agent 当前实例所属的 task_call_id（None = 未通过 task 调用）
agent_last_task: dict[str, str | None] = {}
# agent_name → 已分配的最高实例序号
agent_invocation_counter: dict[str, int] = {}
# instance_node_id → 首个事件的时间戳
instance_first_ts: dict[str, str] = {}
# instance_node_id → 末个事件的时间戳
instance_last_ts: dict[str, str] = {}
```

### _agent_node_id 变更

从独立函数 → `_ProjectionState` 方法：

```python
def _agent_node_id(self, event: TraceLogEvent) -> str:
    if not event.agent_name:
        return "run"
    if _agent_role(event.agent_name) == "main":
        return f"agent:{event.agent_name}"
    # Subagent：查实例表
    invocation = self.agent_invocation_counter.get(event.agent_name)
    if invocation is not None:
        return f"agent:{event.agent_name}:{invocation}"
    # D9 回退：从未通过 task 调用
    return f"agent:{event.agent_name}"
```

---

## 核心算法

### 1. task 事件拦截（主循环开头）

```python
# TraceProjector.project() 主循环中，所有正常逻辑之前
for event in events:
    # ── N2: task 工具拦截 ──
    if event.tool_name == "task" and event.type in {"tool_start", "tool_end", "tool_error"}:
        if event.type == "tool_start":
            state.current_task_call_id = event.tool_call_id
            # 清理并行组追踪（防止泄漏）
            if event.tool_call_id:
                parallel_tc_to_group.pop(event.tool_call_id, None)
        else:  # tool_end / tool_error
            state.current_task_call_id = None
        continue  # ← 不创建 tool 节点、不创建 context

    # ── 正常事件处理 ──
    if event.type in {"llm_start", "llm_end", ...}:
        state.ensure_agent_node(event)
    ...
```

### 2. ensure_agent_node 实例化拆分

```python
def ensure_agent_node(self, event: TraceLogEvent) -> None:
    if not event.agent_name:
        return

    # ── Meta-agent：保持全局唯一 ──
    if _agent_role(event.agent_name) == "main":
        node_id = f"agent:{event.agent_name}"
        if node_id in self.agent_nodes:
            return
        self.agent_nodes.add(node_id)
        self.projection.nodes.append(TraceNode(
            node_id=node_id, parent_node_id="run",
            kind="agent", label=event.agent_name, ...
        ))
        return

    # ── Subagent：实例化拆分 ──
    current_task = self.current_task_call_id
    last_task = self.agent_last_task.get(event.agent_name)

    if last_task is not None and last_task == current_task:
        return  # 同一实例内，不需要新节点

    # 新实例：不同 task_call_id 或首次出现
    counter = self.agent_invocation_counter.get(event.agent_name, 0) + 1
    self.agent_invocation_counter[event.agent_name] = counter
    self.agent_last_task[event.agent_name] = current_task

    node_id = f"agent:{event.agent_name}:{counter}"
    self.agent_nodes.add(node_id)
    self.instance_first_ts[node_id] = event.timestamp

    # 确定父节点
    if _is_evaluation_agent(event.agent_name):
        primary_name = _evaluation_primary_agent_name(event.agent_name)
        primary_inv = self.agent_invocation_counter.get(primary_name, 1)
        parent_id = f"agent:{primary_name}:{primary_inv}"
    else:
        parent_id = "run"

    self.projection.nodes.append(TraceNode(
        node_id=node_id, parent_node_id=parent_id,
        kind="agent", label=event.agent_name, ...
        started_at=event.timestamp,
    ))
```

### 3. 实例时间戳追踪（主循环内）

```python
# 每个事件处理后，更新实例的最后时间戳
if event.agent_name and _agent_role(event.agent_name) == "subagent":
    instance_id = state._agent_node_id(event)
    state.instance_last_ts[instance_id] = event.timestamp
```

### 4. Duration 计算（主循环结束后）

```python
# 遍历 agent 节点，填充 ended_at / duration_ms
for node in projection.nodes:
    if node.kind != "agent":
        continue
    first = state.instance_first_ts.get(node.node_id)
    last = state.instance_last_ts.get(node.node_id)
    if first and last:
        node.ended_at = last
        node.duration_ms = _ts_diff_ms(first, last)
```

### 5. N4 上下文过滤（前端渲染时）

**后端 projector 不做任何过滤**——照常为所有 agent（包括 meta-agent）生成 context 段落。
`_append_context` 保持原有逻辑不变。

过滤逻辑在前端 **TracePanel** 组件层：

```typescript
// TracePanel / 上下文面板组件中
// 根据 activeNode（当前选中节点）决定过滤策略
const visibleSegments = useMemo(() => {
  if (!activeNode) return context;
  // 选中 meta-agent 节点 → 不过滤，展示全部
  if (activeNode.agent_role === "main") return context;
  // 选中 subagent 节点 → 过滤掉 main agent 的段落
  return context.filter(seg => seg.agent_role !== "main");
}, [context, activeNode]);
```

行为：
- 点击 **subagent 节点**（writing #1） → 右侧抽屉只显示 subagent 的上下文，过滤掉 meta-agent 的系统提示/规划
- 点击 **meta-agent 节点** → 右侧抽屉展示 meta-agent 的完整上下文（不过滤）

### 6. 前端 nodeBodyLabel 显示序号

```typescript
function nodeBodyLabel(node: TraceNode): string {
  if (node.kind === "agent") {
    const name = node.agent_name || "Agent";
    // 从 node_id 提取实例序号
    const match = node.node_id.match(/^agent:.+:(\d+)$/);
    if (match) {
      const display = name.replace(/-subagent$/, "");
      return `${display} #${match[1]}`;
    }
    return name;
  }
  // ... 其他 kind 不变 ...
}
```

---

## 接口契约

### TraceNode 变更

**不新增字段**。所有实例信息通过 `node_id` 格式编码：
- `node_id = "agent:writing-subagent:1"` → 前端正则 `/:([^:]+):(\d+)$/` 提取序号
- `duration_ms` 现在对 agent 节点也有值（之前为 None）

### 前端 trace.ts 同步变更

前端 projector 需镜像以下变更：
1. `agentNodeId()` → 改为闭包内局部函数，访问实例追踪状态
2. `ensureAgentNode()` → 增加实例化逻辑（与后端 `ensure_agent_node` 对称）
3. 主循环 → 增加 task 事件拦截（`event.tool_name === "task"` 时 continue）
4. Duration → 主循环结束后回填 agent 节点的 `duration_ms`

### 前端 TracePanel N4 过滤

- TracePanel 组件增加 `activeNode` prop 或从 context 获取
- 上下文段落列表根据 `activeNode.agent_role` 过滤（见算法 5）

---

## 任务拆解 (WBS)

### Phase 1: 后端 projector.py 变更

| # | Task | 文件 | 依赖 | 验证标准 |
|---|------|------|------|----------|
| T1.1 | 新增实例追踪状态字段 | `projector.py` `_ProjectionState.__init__` | 无 | 字段初始化正确 |
| T1.2 | `_agent_node_id` 从函数改为方法 | `projector.py` | T1.1 | 所有调用点（add_llm_node 等）改为 `state._agent_node_id()` |
| T1.3 | `ensure_agent_node` 实例化拆分 | `projector.py` | T1.1, T1.2 | 同名多次调用产生独立节点 |
| T1.4 | 主循环 task 事件拦截 | `projector.py` `project()` | T1.1 | task 事件不产生 tool 节点 |
| T1.5 | 实例时间戳追踪 + duration 回填 | `projector.py` | T1.3, T1.4 | agent 节点有 duration_ms |

### Phase 2: 前端 trace.ts 变更

| # | Task | 文件 | 依赖 | 验证标准 |
|---|------|------|------|----------|
| T2.1 | 镜像 T1.1-T1.5 所有变更 | `trace.ts` | T1.* | 流式视图与 API 视图一致 |
| T2.2 | `nodeBodyLabel` 显示序号 | `TraceChainTimeline.tsx` | T2.1 | agent 节点显示 "writing #1" 等 |
| T2.3 | N4 上下文渲染过滤 | `TracePanel.tsx` | 无 | 选中 subagent 时过滤 main agent 段落 |

### Phase 3: 验证

| # | Task | 验证点 |
|---|------|--------|
| T3.1 | 多章 writing trace 回放 | N1: 每章独立折叠条目；N2: 无 task 节点；N3: 有耗时 |
| T3.2 | evaluation 嵌套 | D8: evaluation 正确嵌套在 writing 实例内 |
| T3.3 | meta-agent 上下文过滤 | N4: 选 subagent 时抽屉无 main 段落；选 meta-agent 时正常展示；N5: 时间线保留 meta 节点 |
| T3.4 | D9 回退 | 无 task 事件的 subagent → 全局唯一节点，不崩溃 |
| T3.5 | SSE 流式一致性 | 流式追加的视图与刷新后 API 视图结构一致 |
