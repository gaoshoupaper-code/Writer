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

### 决策 3：evolution 输出 —— 结构化评分 + 修改建议

evolution 返回结构化评估结果（评分 + 具体修改建议），DeepAgent 根据评估结果自主决定是否修订。

### 决策 4：修订无上限

取消现有 pipeline 的 `max_revision_count=3` 硬上限。DeepAgent 自主决定何时停止修订。

---

## 待澄清

1. 无限修订的终止机制（防止死循环）
2. character 子代理的 evolution prompt 来源
3. 现有 pipeline 基础设施（_build_compiled_pipeline_subagent 等）的处理方式
4. 各子代理 DeepAgent 的 middleware 配置
5. evolution subagent 的权限范围
