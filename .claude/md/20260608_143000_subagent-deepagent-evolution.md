---
type: require
status: draft
created: 2026-06-08 14:30
source: 把 character/detail_outline/outline/writing 四个 subagent 从 pipeline 改为 DeepAgent 架构，内含 evolution subagent
related: []
---

# 核心诉求

将 outline、detail_outline、writing、character 四个子代理从当前架构（前三个为硬编码 StateGraph pipeline，最后一个为纯 SubAgent dict）改为 **DeepAgent 架构**。每个 DeepAgent 内部自主决策生成/修改流程，并内置一个 **evolution subagent**，在每次生成或修改后自动进行评估演化。

## 当前架构（要被替换的）

```
meta_agent (create_deep_agent)
  ├── general-purpose (SubAgent)
  ├── character (SubAgent dict，无评估)
  ├── outline (CompiledSubAgent - StateGraph pipeline)
  │     primary → validate → evaluation → validate → [revision loop] → final
  ├── detail_outline (CompiledSubAgent - StateGraph pipeline)
  │     primary → validate → evaluation → validate → [revision loop] → final
  └── writing (CompiledSubAgent - StateGraph pipeline)
        primary → validate → evaluation → validate → [revision loop] → final
```

## 已确认决策

### 决策 1：架构选型 —— 每个子代理本身变为 `create_deep_agent`

每个子代理（outline/detail_outline/writing/character）内部使用 `create_deep_agent` 创建，
拥有自己的 subagent（evolution）、自己的 context 管理和 tool 调用循环。

目标架构：
```
meta_agent (create_deep_agent)
  ├── general-purpose (SubAgent)
  ├── character (create_deep_agent)
  │     └── evolution (SubAgent - 评估角色档案)
  ├── outline (create_deep_agent)
  │     └── evolution (SubAgent - 评估大纲)
  ├── detail_outline (create_deep_agent)
  │     └── evolution (SubAgent - 评估细纲)
  └── writing (create_deep_agent)
        └── evolution (SubAgent - 评估正文)
```

### 决策 2：evolution 来源 —— 迁移现有 evaluation_subagent

evolution subagent 不是新建，而是将现有 `evaluation_subagent`（及其 3 套 prompt：
outline_evaluation、detail_outline_evaluation、review_evaluation）迁移进每个 DeepAgent 内部。
character 子代理新增对应的 evolution prompt。

### 决策 3：evolution 输出 —— 沿用现有 evaluation schema

evolution subagent 沿用现有 evaluation_subagent 的 structured output schema：
`{score, suggestion, issues, revision_instruction, quality_risk}`。
DeepAgent 读取这些字段自主决定是否修订。

### 决策 4：修订上限 —— 硬性 3 轮，自定义 middleware 计数

保留硬性上限 3 轮修订。通过自定义 `RevisionLimitMiddleware` 拦截 evolution subagent 的调用次数，
超过 3 次后注入系统消息"已达修订上限，请接受当前版本"。不依赖 system prompt 约束。

### 决策 5：evolution 权限 —— 可读可写

evolution subagent 可以读取文件进行评估，也可以将评估报告写入文件（如 `evaluation.md`），
同时通过 structured output 返回摘要给父 DeepAgent。

### 决策 6：Middleware —— 完全依赖 create_deep_agent 默认

不手动组装 middleware。仅通过 `create_deep_agent` 的 `middleware=` 参数
额外注入项目特有的：PathGuard、Trace、Goal、ErrorRecovery、RevisionLimitMiddleware、ArtifactValidationMiddleware。

### 决策 7：Pipeline 代码 —— 直接删除

`_build_compiled_pipeline_subagent` 及其相关的 200+ 行通用管道代码（StateGraph 定义、
validate 节点、revision 节点、路由函数等）全部废弃删除。

### 决策 8：产物文件校验 —— 改为 middleware

现有 pipeline 的 validate 逻辑（检查产物文件存在且非空）改为 `ArtifactValidationMiddleware`，
在文件写入后自动校验。

### 决策 9：Character evolution —— 新写评估 prompt，5 维度全覆盖

为 character 子代理新写一个专用的 evolution 评估 prompt，覆盖以下 5 个评估维度：
- 角色弧光完整性（起点→转变→终点）
- 动机合理性（行动有内在驱动，非被剧情推着走）
- 关系网络一致性（角色间关系无矛盾，与设定无冲突）
- 差异化程度（多角色间有足够个性区分，避免脸谱化）
- 整体质量（作为兜底）

### 决策 10：Checkpointer —— 共用 + thread_id 前缀隔离

所有子代理共用 meta_agent 的同一个 checkpointer，通过 thread_id 前缀隔离状态
（如 `outline-{thread_id}`、`writing-{thread_id}`）。

### 决策 11：Meta agent system prompt —— 同步更新

修改 meta_agent 的 system prompt，将基于 pipeline 的措辞更新为适配 DeepAgent 架构的描述。
子代理从"被动执行 pipeline"变为"自主决策的 DeepAgent"，meta_agent 的指令方式需相应调整。

---

## 覆盖全景检查

| 关注面 | 状态 | 决策编号 |
|--------|------|----------|
| 整体架构选型 | ✅ 已覆盖 | 决策 1 |
| evolution 来源与迁移 | ✅ 已覆盖 | 决策 2 |
| evolution 输出格式 | ✅ 已覆盖 | 决策 3 |
| 修订上限与执行机制 | ✅ 已覆盖 | 决策 4 |
| evolution 权限范围 | ✅ 已覆盖 | 决策 5 |
| Middleware 策略 | ✅ 已覆盖 | 决策 6 |
| 旧代码处理 | ✅ 已覆盖 | 决策 7 |
| 产物文件校验 | ✅ 已覆盖 | 决策 8 |
| Character evolution prompt | ✅ 已覆盖 | 决策 9 |
| 状态持久化（checkpointer） | ✅ 已覆盖 | 决策 10 |
| Meta agent prompt 适配 | ✅ 已覆盖 | 决策 11 |
| evolution 调用失败容错 | ⚪ 不适用 | 沿用现有行为：失败则子代理失败，由 meta_agent 的 ErrorRecoveryMiddleware 处理 |
| Streaming 兼容性 | ⚪ 不适用 | `create_deep_agent` 原生支持 streaming，meta_agent 的 `astream_events` 自动适配嵌套事件 |
