---
name: auto-pipeline
description: 当 demand.md 的 mode 为 auto（用户选择全自动生成）时使用。demand 成型后无需用户操作，自动串联 storybuilding→detail_outline→writing 生成完整小说，不暂停等确认。
---

# 全自动流水线（auto 模式）

本 skill 指导你在 demand.md 确认成型（status=confirmed, mode=auto）后，**无需等待用户**，自动推进到底。

## 流程

1. **故事构建**（2-10 轮）：委托 storybuilding 初构（skeleton skill）→ 多轮增量（expand skill）。每轮传入本轮焦点与前轮审查问题。
2. **细纲**：委托 detail-outline 先生成 detail/overview.md 获取总章数，再按顺序逐章生成 detail/chapter-XX.md。
3. **正文**：委托 writing 逐章写作；每章返回后再写下一章。

## 关键纪律

- **不暂停等待用户确认**。各阶段审查与修订由子代理内部自动处理（review 单次审查修订）。
- 若子代理返回质量风险，优先处理当前问题再推进，不要为了进度跳过有问题的章节。
- 每完成一个子代理委托，立即推进下一个，直到全部章节完成。

## 最终回复

全流程结束后，用 2-4 句话简要说明完成的创作结果和关键设定。
