---
name: detail-planning
description: >-
  【每次细纲任务开始前必读】章节细纲规划执行流程。读 timeline.md 取下一批事件，
  自主分章，写入 chapter-XX.md 并增量更新 overview.md，完成后调用 review 审查（单次）。
  本 Skill 是细纲任务的唯一执行入口，跳过它直接开写属于违规。
---

# detail-planning

章节细纲规划执行流程。读全局事件时间线，按固定事件批次自主分章，完成后调用 review 审查（单次）。

> **必读约束**：本 Skill 是细纲任务的唯一执行入口。系统提示词已要求你「每次细纲任务开始前必须先读取本 Skill 并遵循其流程」。请完整阅读下方流程后再开始操作，不得跳过任何步骤。

## 执行流程

当父代理要求生成本批细纲时：

1. **定位进度**：读 detail/overview.md（若存在），找到最后已生成章号 + 最后消费的 timeline 序号。若 overview.md 不存在（首次），从 timeline 序号 1 开始。
2. **取事件批次**：读 storyline/timeline.md，取（最后消费序号 + 1）开始的下一批 5-8 个事件。
3. **读取上下文**：
   - storyline/timeline.md：本批事件的全局时间序、类型、各线归属
   - character/*.md：出场人物设定
   - detail/ 已有 chapter-XX.md：章节承接、近期章末钩子类型
4. **自主分章**：在批次内自主决定分几章、每章几事件（一章可承载多个事件）。保证每章有核心焦点和主爽点，禁止整章纯过渡。
5. **写入产物**：
   - 依次写入 detail/chapter-XX.md（全局连续编号，格式遵循 Prompt 中的结构规范）
   - 增量追加新章行到 detail/overview.md（不整表重写）

完成当前批次后**立即返回**，等待父代理下一个指令。不自行开始下一批。

## review 审查（单次）

完成本批写入后，调用 review 审查（**全流程只调用 1 次**）：

1. 调用 review 子代理审查本批章节质量（时间序正确性、爽点有效性、节奏疏密、人物一致性、与 timeline 一致性）。**调用时 description 必须明确列出待审查文件的完整路径**（前置上下文不含待审查文件，review 需自行 `read_file`）。格式示例：
   ```
   审查本批细纲质量。待审查文件：
   - /detail/chapter-15.md
   - /detail/chapter-16.md
   对照基准：/storyline/timeline.md、/character/*.md。
   ```
2. review `read_file` 读取上述 detail/ 文件 + storyline/timeline.md，写审查报告到 review/detail.md，返回评分和修改建议。
3. 根据 review 返回结果：
   - "无需修改" → 直接返回。
   - "建议修改" / "必须修改" → 读 review/detail.md，修订本批章节文件**一次**，不再二次审查，直接返回。
4. 返回父代理时回复包含：是否执行修订、是否有质量风险、本批 timeline 事件序号范围、本批章节号范围。
   - 格式示例：`执行修订：否\n质量风险：无\n事件范围：timeline 序 6~10\n章节范围：chapter-04 ~ chapter-06`

## 注意事项

- **timeline 是唯一时间序依据**：按 timeline "序"列分章，不读各 story 线详情自行排序。
- **不固定章数**：每批 5-8 事件，自主决定分几章，一批可能 2-4 章。
- **一章可多事件**：连续小事件可合并一章。
- overview.md 增量追加，不整表重写；它同时是下次调用的进度依据。
- 若发现 timeline 有遗漏事件或不一致，在回复中提出建议，不自行修改 timeline（那是 storybuilding 的产物）。
