---
name: interactive-gating
description: 当 demand.md 的 mode 为 interactive（用户选择逐步审查）时使用。每个阶段（storybuilding/detail_outline/writing）完成后暂停，向用户展示成果并等待确认才推进。
---

# 阶段门控（interactive 模式）

本 skill 指导你在 demand.md 确认成型（status=confirmed, mode=interactive）后，按阶段门控推进：每个阶段完成暂停等用户确认。

## 流程

1. **故事构建**：委托 storybuilding 初构/增量。完成后**暂停**，展示大纲摘要，等用户确认。
2. **细纲**：用户确认后，委托 detail-outline 逐章生成。完成后**暂停**，展示细纲概览，等确认。
3. **正文**：用户确认后，委托 writing 逐章写作。

## 阶段门控

**阶段之间必须暂停，等待用户确认后才可推进。禁止在一个请求内串联多个阶段。**

| 阶段转换 | 行为 |
|---|---|
| storybuilding 完成 | **暂停**，展示大纲摘要，提示审查 |
| detail-outline 完成 | **暂停**，展示细纲概览，提示审查 |
| 用户确认（"继续"/"可以"/"开始写正文"） | 推进到下一阶段 |
| 用户提出修改意见 | 委托对应子代理修订，修订后**再次暂停**等确认 |

## 最终回复

Markdown 格式，只做概述，不输出完整剧情/大纲/正文。阶段暂停时必须包含审查提示，引导用户确认或提修改意见。
