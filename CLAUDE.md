# CLAUDE.md

## 文档同步铁律（最高优先级）

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

### 文档同步自检清单（每个任务收尾必走）

**第 1 步：列出本次改了哪些文件**
把所有被改动 / 新增 / 删除的文件列出来。

**第 2 步：把改动文件映射到 docs/ 对应篇目**
按下表判断每处改动该检查哪篇文档：

| 改动类型 | 该检查 |
|---------|--------|
| 改了 executor 任何文件 | `docs/executor/文件大地图.md` |
| 改了 evolution 任何文件 | `docs/evolution/文件大地图.md` |
| 改了 frontend 任何文件 | `docs/frontend/文件大地图.md` |
| 改了系统级结构（跨端、顶层目录、main.py） | `docs/README.md`（系统全景） |
| 改了 writing 域编排逻辑/流程 | `docs/executor/文件大地图.md`（写作流水线分组） |
| 改了 writing 域支撑组件（风格/护栏/服务/工具） | `docs/executor/文件大地图.md`（写作流水线分组） |
| 新增/删除/改了某文件本身的职责 | 该端的**文件大地图.md 必须更新** |
| 只改了提示词内容（prompts/*.md）或技能内容（skills/） | 通常无需改 docs（内容非结构）；**除非增删整个文件 → 更新文件大地图** |

**第 3 步：对每篇该检查的文档，标注三态之一**

- `已更新`：你同步了文档内容，使其反映本次改动。
- `无需更新`：你确认过，本次改动不影响该篇（即使代码变了，文档描述仍准确）。
- `待补`：需要更新但本次没做完——**必须说明原因**，不能默默放过。

### 收尾汇报（D6）

自检清单走完后，在任务收尾回复里用**一句话**告诉用户：
"本次更新了 docs/ 下：[文件名列表]"（或"无需更新"）。
让用户知道文档动了哪，可随时抽查。

### 写作规范（更新文档时遵守）

- **活文档**：只写"现在的样子"，不写"本次改了什么"。不记 changelog。
- **语言风格：白话但不口语化**。用直白易懂的表述讲清楚逻辑，但保持书面语的专业感——
  避免口水话、网络用语、随意感叹（不要"其实吧""超级""哦"这类词）。
- **大白话逻辑为主**：正文用直接陈述 + 因果逻辑 + 层次递进（"X 做 A，产出 B，传给 Y 做 C"），语言精炼不说冗余过度的词语。
- **术语括号注释**：专业术语**第一次出现时**括号注大白话，
  如"路由（router，就是分发请求的交警）"。同篇重复出现不再注。
- **尽量少用类比**：类比只在纯逻辑实在讲不清时偶尔用一处，不作为主要表达手段。
- **文件级掌控（L2 颗粒度）**：文件地图里每个文件的作用**至少写两三句话**——
  要说清它**干什么 + 为什么需要它 / 在流程里解决什么问题**，不能只一句话带过。
  不列 `__init__.py`（仅为 Python 包标记，无业务逻辑）。
- **文件大地图用卡片式（重要范式）**：三大端的文件大地图用"分区卡片"组织，不是 ASCII 目录树。
  规范：每个逻辑分组（按目录或按职责）= 一张卡片，卡片之间用 `---` 分隔；每张卡片顶部用
  `> **这组管什么**：...` 一句话说清该组的定位；下面是该组的文件表（列：文件/行数/作用）。
  大文件（如 meta/agent.py 1161 行、projector.py 900 行）在作用描述里点明体量和重要性。
  每张大地图末尾附"文件体量速览"条形图，让大头一眼可见。
- **不自造结构**：严格按"文档结构"的层级和命名，不随意新建文档。

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


## 官网字体排版（website/）

官网用**本地子集化的 Noto Serif SC**（思源宋体），不是 Google Fonts 外链。

**字体是怎么工作的**：
- `public/fonts/noto-serif-sc/{regular,semibold}/` —— cn-font-split 切成的 68 个
  unicode-range 分片（每片 ~100KB），浏览器只按页面实际用到的字按需拉取。
- `public/styles/base.css` —— 整站排版 token（字号/字重/字距/行高）+ 颜色 + body reset，
  两个页面（index / download）用 `<link>` 引入。**改字号/排版只改这一个文件。**
- `scripts/split-font.mjs` —— 子集化脚本，读 `@fontsource/noto-serif-sc` 的完整 woff2，
  切成分片输出到 public/fonts/。

**改了文案 / 加了新中文字后**：可能出现某些字 fallback 到系统字体（混字体）。
这时重跑 `node scripts/split-font.mjs`（约 10 秒），重新生成分片即可。
源字体在 `node_modules/@fontsource/noto-serif-sc/files/`，由 devDependency 提供。

**排版 token 体系**（都在 base.css）：
- 字号 scale 用 Major Third 1.2 模数：`--fs-meta`(12) → `--fs-body`(16) → `--fs-display`(clamp 48~69)
- 字重只有 400/500/600，**不用 900**（中文衬线 900 发死）
- 字距：标题用零或正字距，**不用负字距**（负字距压方块字，是原"不好看"的主因）


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

