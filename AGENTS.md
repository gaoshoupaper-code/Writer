# AGENTS.md

## 文档同步铁律

**必须遵守**：任何开发任务（写新功能 / 修 bug / 重构 / 改配置）收尾前，**必须**执行
下方的【文档同步自检清单】。**未执行完不得宣告任务完成。**

### 背景

本项目在 `docs/` 下维护一套"活文档"（living doc）——它只反映项目**当前的样子**，
不记变更历史。文档是产品负责人（A 角色，非开发者）掌控项目的唯一依靠。
文档一旦和代码对不上，掌控就断了。因此每次改动代码后，必须同步检查文档是否仍准确。

### 文档结构（你要维护什么）

```
docs/
├── README.md                       ← 系统全景（整个系统一张图 + 三大端下钻入口）
├── executor/文件大地图.md           ← executor 每个文件（目录树+作用，含 writing 域全部文件）
├── evolution/文件大地图.md          ← evolution 每个文件（目录树+作用）
├── frontend/文件大地图.md           ← frontend 每个文件（目录树+作用）
```
**核心约定**：三大端的"文件大地图"是看全局的地图——按真实目录树组织，
每个文件标两三句精炼作用。改了某端的文件，必须同步该端的文件大地图。

### 收尾汇报（D6）

自检清单走完后，在任务收尾回复里用**一句话**告诉用户：
"本次更新了 docs/ 下：[文件名列表]"（或"无需更新"）。
让用户知道文档动了哪，可随时抽查。

---

## 部署与验证流程

**开发完成后直接 push 到仓库，然后直接看上线效果，不在本地看效果。**

- 本地不跑效果验证，默认 CI/CD 部署后直接观测线上行为。
- push 后等部署完成，去线上确认本次改动是否符合预期，若不符合再迭代。

---

Behavioral guidelines to reduce common LLM coding mistakes.
Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed.
For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Robust & Clean First, Simple Second

**功能架构健壮完善优先，代码干净整洁，其次简洁。** Architecture must hold up; the code must be clean; simplicity serves both.

**First, make it robust and complete:**
- Handle the edge cases the problem *actually* has—don't skip them for brevity.
- Get the architecture right: clear module boundaries, correct responsibilities, and abstractions only where the domain genuinely needs them.
- Correctness and structural soundness outweigh line count.

**Keep it clean—no dead weight, no clutter:**
- No dead code, unused imports, or leftover scaffolding. If it's not doing work, it's gone.
- One responsibility per module/function; no tangled side effects or hidden state.
- Naming and structure that say what the code does—reader shouldn't have to reverse-engineer it.
- Remove the mess your changes create (orphans, stale comments, now-pointless branches) as you go.

**Then, make it as simple as robustness allows:**
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If 200 lines could be 50 *without losing robustness*, rewrite it.

The order is fixed: **robust & clean → simple**. Never trade correctness, structural soundness, or clarity for fewer lines.
Ask yourself: "Is it robust and complete? Is it clean? Is it as simple as it can be without sacrificing either?" All three must be yes.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]

Strong success criteria let you loop independently.
Weak criteria ("make it work") require constant clarification.

---
**These guidelines are working if:** fewer unnecessary changes in diffs,
fewer rewrites due to overcomplication, and clarifying questions come
before implementation rather than after mistakes.


## 后端
框架必须使用DeepAgent
如果有不清楚的，或有必要的话，去查阅官网文档https://docs.langchain.com/oss/python/deepagents/overview
使用中文思考回答

## 云服务器
ssh writer


## Coding & Interaction Style

When we are working together on code, troubleshooting, or discussing concepts, please act as a senior mentor. Use a **guided walkthrough approach** focused on helping me 100% understand the "why" behind every decision.

Please adhere to the following rules based on the task:

**1. When Writing New Code:**
- **Reasoning First:** Briefly explain the architectural choice or logic *before* writing the code (Why this approach? What are the trade-offs?).
- **Incremental Delivery:** Deliver code in small, digestible bites—one function, component, or logical unit at a time.
- **"Why" Comments:** Add comments only at complex, clever, or unintuitive logic points to explain *why* it's written that way, not *what* it does.
- **Pacing:** After each segment, pause and confirm my understanding before moving to the next step.

**2. When Fixing Bugs or Refactoring:**
- **Root Cause Analysis:** Never just paste the fixed code. First, clearly explain the root cause—*why* did it break, or *why* is the current approach suboptimal?
- **The Fix Strategy:** Briefly outline how you plan to fix it.
- **Focused Changes:** Provide only the specific lines or functions that need to change, rather than dumping the entire file.

**3. When Answering Questions or Explaining Concepts:**
- **Direct First:** Start with a clear, direct answer or definition.
- **Mental Models:** If the concept is abstract, complex, or low-level, use simple, real-world analogies to make it intuitive.
- **Check for Clarity:** End your explanation by asking if the analogy made sense or if I need you to break down a specific part further.
