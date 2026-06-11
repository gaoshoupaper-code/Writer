# LLM Wiki 学习文档

> Karpathy 的 LLM Wiki 模式精读与评估
> 
> 素材来源：[Karpathy 原始 Gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)（2026-04-04）
> 
> 整理时间：2026-06-11

---

## 目录

1. [LLM Wiki 是什么](#1-llm-wiki-是什么)
2. [原始 Gist 精读](#2-原始-gist-精读)
3. [设计哲学](#3-设计哲学)
4. [评估：能否融入现有工作流](#4-评估能否融入现有工作流)
5. [评估：长期维护成本与可靠性](#5-评估长期维护成本与可靠性)
6. [结论](#6-结论)
7. [实操指南：从零搭建你的 LLM Wiki](#7-实操指南从零搭建你的-llm-wiki)

---

## 1. LLM Wiki 是什么

### 1.1 问题背景

大多数人使用 LLM + 文档的方式是 **RAG（检索增强生成）**：上传一批文件，LLM 在查询时检索相关片段，生成回答。NotebookLM、ChatGPT 文件上传、大多数企业 AI 知识库都走这条路。

**RAG 的根本问题**：LLM 在每次提问时都从头重新发现知识，没有积累。问一个需要综合五份文档的问题，LLM 每次都得重新找、重新拼。明天再问同样的问题，它重复同样的工作。**没有任何东西被构建出来。**

### 1.2 核心思想

Karpathy 的 LLM Wiki 做法完全不同：

> **不让 LLM 在查询时从原始文档检索，而是让 LLM 增量地构建和维护一个持久的 Wiki**——一个结构化的、互链的 Markdown 文件集合，位于你和原始资料之间。

当你添加新资料时，LLM 不是为后续检索做索引，而是：读取资料、提取关键信息、**整合进现有 Wiki**——更新实体页、修订主题摘要、标注新旧数据矛盾、强化或挑战正在演进的综述。

**关键区别：Wiki 是一个持久的、复利增长的产物。** 交叉引用已经建好了。矛盾已经标记了。综述已经反映了你读过的所有内容。每添加一个来源、每问一个问题，Wiki 都变得更丰富。

### 1.3 一句话总结

**Obsidian 是 IDE，LLM 是程序员，Wiki 是代码库。**

你负责策展资料、探索和提问；LLM 负责所有苦力活——摘要、交叉引用、归档和记账。

---

## 2. 原始 Gist 精读

### 2.1 三层架构（深入详解）

Karpathy 定义了三个清晰的层级。这不是简单的目录划分，而是一套**权责分离的信息架构**——每一层有明确的"谁拥有、谁能写、谁负责"。

#### 2.1.1 全局目录结构

```
project/
├── CLAUDE.md          ← 第 3 层：Schema（规则文档）
├── raw/               ← 第 1 层：原始资料（不可变）
│   ├── articles/      ← 网页文章（Markdown）
│   ├── papers/        ← 论文（PDF 或 Markdown）
│   ├── repos/         ← 代码仓库的 README / 笔记
│   ├── data/          ← CSV、JSON 等结构化数据
│   └── assets/        ← 本地化图片、截图
└── wiki/              ← 第 2 层：LLM 生成的 Wiki（LLM 拥有）
    ├── index.md       ← 内容目录（每次 ingest 更新）
    ├── log.md         ← 活动日志（追加写入）
    ├── overview.md    ← 高层综述（所有来源的综合）
    ├── concepts/      ← 概念页（如"注意力机制"）
    ├── entities/      ← 实体页（如"OpenAI"、"Transformer"）
    ├── sources/       ← 来源摘要（每个 raw 文件的摘要）
    └── comparisons/   ← 对比分析（跨来源对比）
```

#### 2.1.2 第 1 层：Raw Sources（原始资料层）

**一句话：你的真相源，神圣不可侵犯。**

```
raw/
├── articles/
│   ├── 2026-03-attention-is-all-you-need-revisited.md
│   └── 2026-04-scaling-laws-update.md
├── papers/
│   ├── transformer-architecture-v2.pdf
│   └── mixture-of-experts-survey.pdf
├── repos/
│   ├── llama-3-readme.md
│   └── vllm-architecture-notes.md
├── data/
│   ├── benchmark-results.csv
│   └── model-comparison.json
└── assets/
    ├── transformer-diagram.png
    └── scaling-curves.png
```

| 属性 | 规则 |
|------|------|
| **所有权** | 你拥有。你是唯一有权往里放东西的人 |
| **读权限** | LLM 可读 |
| **写权限** | LLM **绝不写入**。Raw 一旦放入，即成为不可变的历史记录 |
| **格式** | 任意——Markdown、PDF、图片、CSV、JSON |
| **命名建议** | 带日期前缀（如 `2026-04-02-article-title.md`），便于排序和溯源 |

**为什么必须不可变？**

这是整个系统的**信任锚点**。Wiki 是 LLM 生成的衍生品，它可能出错。当你怀疑 Wiki 中的某个声明时，你需要一个可靠的、未经 AI 手的原始版本来核实。如果 LLM 也能写 Raw，那这个信任链就断了——你无法区分"原始事实"和"AI 的理解"。

**实际操作中的要点**：
- 用 Obsidian Web Clipper 把网页文章剪藏为 Markdown，直接存到 `raw/articles/`
- 论文 PDF 放到 `raw/papers/`（LLM 可以读 PDF）
- 图片需要手动下载到 `raw/assets/`，然后在文章中引用本地路径
- 每个文件建议加 YAML frontmatter 记录来源 URL、作者、日期

```markdown
---
url: https://example.com/article-title
author: John Doe
date: 2026-04-02
tags: [ml, transformer, attention]
---

# 文章标题

（原始内容...）
```

#### 2.1.3 第 2 层：The Wiki（知识库层）

**一句话：LLM 的地盘，LLM 写，你读。**

```
wiki/
├── index.md                    ← 最重要的导航文件
├── log.md                      ← 追加式活动日志
├── overview.md                 ← 高层综合综述
├── concepts/
│   ├── attention-mechanism.md  ← 概念：注意力机制
│   ├── mixture-of-experts.md   ← 概念：MoE
│   └── scaling-laws.md         ← 概念：缩放定律
├── entities/
│   ├── openai.md               ← 实体：OpenAI
│   ├── anthropic.md            ← 实体：Anthropic
│   └── google-deepmind.md      ← 实体：Google DeepMind
├── sources/
│   ├── summary-attention-revisited.md  ← 文章摘要
│   └── summary-scaling-update.md       ← 文章摘要
└── comparisons/
    ├── gpt4-vs-claude-vs-gemini.md     ← 对比
    └── rag-vs-finetuning.md            ← 对比
```

| 属性 | 规则 |
|------|------|
| **所有权** | LLM 拥有。LLM 创建、更新、维护所有内容 |
| **读权限** | 你和 LLM 都可读 |
| **写权限** | **原则上只有 LLM 写**。你可以在 Obsidian 中浏览、但不应该直接编辑——让 LLM 做修改，保持一致性 |
| **格式** | 纯 Markdown，带 YAML frontmatter |
| **链接** | 使用 `[[wiki-link]]` 双链语法，形成知识图谱 |

**每种页面类型的作用**：

| 页面类型 | 放在哪 | 内容 | 何时创建/更新 |
|----------|--------|------|----------------|
| **来源摘要** | `wiki/sources/` | 一个 Raw 文件的关键要点提炼、你的批注、与已有知识的关联 | 每次 Ingest |
| **概念页** | `wiki/concepts/` | 某个技术概念的定义、变体、关键发现、争议 | 当某个概念被多次提及时创建；每次 Ingest 可能更新 |
| **实体页** | `wiki/entities/` | 人物、组织、项目的画像：他们做了什么、关键事件、时间线 | 当某个实体在多个来源中出现时创建 |
| **对比页** | `wiki/comparisons/` | 跨来源的对比分析（如模型对比、方法对比） | Query 产生有价值的分析时归档 |
| **综述** | `wiki/overview.md` | 整个 Wiki 的高层综述——当前的理解状态、核心论点、开放问题 | 随 Wiki 增长定期更新 |

**index.md 的深层作用——替代 RAG 的秘密**

index.md 不是普通的目录，它是 LLM 的**检索入口**。Query 的工作流不是向量搜索，而是：

```
你的问题 → LLM 读 index.md（几千 token）
         → LLM 从 index 中识别相关页面
         → LLM 读取那些具体页面
         → LLM 综合回答
```

这意味着在中等规模（~100 来源、数百页面）下，**你完全不需要向量数据库、embedding 管线、RAG 基础设施**。一个维护良好的 index.md 就是检索系统。

index.md 的样子：

```markdown
# Wiki Index

## 概念
- [[attention-mechanism]] — 自注意力、多头注意力及其变体（12 个来源）
- [[mixture-of-experts]] — 稀疏 MoE 架构、路由策略（8 个来源）
- [[scaling-laws]] — Chinchilla、Kaplan 定律、计算最优训练（15 个来源）

## 实体
- [[openai]] — GPT 系列、组织历史（20 个来源）
- [[anthropic]] — Claude 系列、Constitutional AI（14 个来源）

## 来源摘要
- [[summary-attention-revisited]] — 2026-03-15
- [[summary-moe-efficiency]] — 2026-04-01

## 对比
- [[moe-routing-strategies]] — 从 Query 归档，2026-04-04
```

**log.md——Wiki 的"Git Log"**

log.md 记录了 Wiki 的演化历史，格式化以便机器解析：

```markdown
# Activity Log

## [2026-04-04] ingest | MoE 效率文章
来源：raw/articles/2026-04-moe-efficiency.md
创建页面：sources/summary-moe-efficiency.md
更新页面：concepts/mixture-of-experts.md, concepts/scaling-laws.md
备注：与 dense-vs-sparse 页面的 <10B 参数声明矛盾，已标记

## [2026-04-04] query | MoE 路由策略对比
问题：对比各来源中 MoE 模型的路由策略
读取页面：concepts/mixture-of-experts.md, 3 篇来源摘要
输出：已归档为 comparisons/moe-routing-strategies.md

## [2026-04-04] lint | 每周健康检查
发现矛盾：2 处
孤儿页面：3 个
建议新建页面：4 个
```

LLM 在每次新会话开始时可以读 log.md 的最后几条，快速了解 Wiki 的当前状态——相当于"上下文恢复"。

#### 2.1.4 第 3 层：The Schema（规则层）

**一句话：让 LLM 从"通用聊天机器人"变成"你的专属 Wiki 维护者"的配置文件。**

| 属性 | 规则 |
|------|------|
| **所有权** | 你和 LLM **共同演化**——你定方向，LLM 帮你细化 |
| **文件名** | 取决于你用的 Agent：Claude Code → `CLAUDE.md`，Codex → `AGENTS.md`，OpenCode → `OPENCODE.md` |
| **更新频率** | 随使用逐步完善，不是一次写死的 |
| **核心作用** | 跨会话的持久指令——即使关掉 Claude Code 重新打开，LLM 仍然知道怎么做 |

**Schema 里到底写什么？它解决的是三个问题**：

```
问题 1："Wiki 长什么样？" → 定义目录结构、页面格式、frontmatter 字段
问题 2："收到新资料怎么办？" → 定义 Ingest 工作流的每一步
问题 3："我该怎么提问？" → 定义 Query 和 Lint 的标准行为
```

Schema 的演化路径：

```
第 1 天（起步）：只写最基本的目录结构和 Ingest 步骤
第 1 周（调整）：发现页面格式需要更多字段，补充 frontmatter 约定
第 2 周（成熟）：加入 Lint 规则、Query 行为、特殊情况处理
第 1 月（稳定）：Schema 基本不再变化，只有偶尔微调
```

Karpathy 说："你和 LLM 共同演化这个文件。"意思是：你发现 Wiki 的某种页面结构不好用，就让 LLM 改 Schema；LLM 在执行中遇到歧义，也会向你确认后更新 Schema。

**Schema 为什么比 CLAUDE.md 的其他用法更关键？**

普通的 CLAUDE.md 告诉 LLM "怎么写代码"。而 LLM Wiki 的 Schema 告诉 LLM "怎么维护一个知识体系"——这是更高层次的行为规范。没有 Schema，LLM 只是一个有文件访问权限的聊天机器人；有了 Schema，它变成一个有纪律、有方法论的 Wiki 维护者。

#### 2.1.5 三层之间的信息流

```
                        你（策展者）
                           │
                    ┌──────▼──────┐
                    │  放入资料    │
                    │  提问/引导   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   Raw 层     │  ← 只写一次，永不修改
                    │  (真相源)    │
                    └──────┬──────┘
                           │ LLM 读取
                    ┌──────▼──────┐
                    │  Schema 层   │  ← 告诉 LLM 怎么做
                    │  (规则手册)  │
                    └──────┬──────┘
                           │ LLM 遵循规则
                    ┌──────▼──────┐
                    │   Wiki 层    │  ← LLM 持续构建和维护
                    │  (知识产物)  │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   你（读者） │  ← 浏览、探索、验证
                    │   + LLM     │  ← 回答你的问题
                    └─────────────┘
```

关键原则：
- **数据单向流动**：Raw → Wiki。信息从 Raw 提取进入 Wiki，但 Wiki 永远不反向修改 Raw
- **控制双向流动**：你 ↔ Schema ↔ LLM。你和 LLM 共同演化 Schema
- **使用双向流动**：你 ↔ Wiki。你浏览 Wiki、提出问题，LLM 基于 Wiki 回答并可能更新 Wiki

### 2.2 三种核心操作

#### 操作一：Ingest（摄入）

你把新资料丢进 `raw/`，告诉 LLM 处理它。流程：

1. LLM 读取来源
2. 与你讨论关键要点
3. 在 `wiki/sources/` 创建摘要页
4. 更新 `wiki/index.md`
5. 更新所有相关的概念页和实体页
6. 在 `wiki/log.md` 追加一条记录

**一次 Ingest 可能影响 10-15 个 Wiki 页面。** 一个新论文可能触发：
- 创建新的论文摘要页
- 更新"注意力机制"概念页（加入新变体）
- 更新"缩放定律"页（加入新 benchmark）
- 更新作者/机构的实体页
- 更新对比页（如果有与已知模型的对比）
- 从已有页面添加指向新内容的链接

Karpathy 的个人偏好：逐个摄入并保持参与——读摘要、检查更新、引导 LLM 侧重点。但也可以批量摄入、减少监督。

#### 操作二：Query（查询）

你向 Wiki 提问。LLM 搜索相关页面、读取、综合回答并附带引用。

输出的形式取决于问题——Markdown 页面、对比表格、幻灯片（Marp）、图表（matplotlib）。

**关键洞察：好的回答可以被回写为新的 Wiki 页面。** 你要求的一次对比、一个分析、一个你发现的联系——这些都是有价值的，不应该消失在聊天历史中。

这形成了一个**复利循环**：
```
来源 → Ingest → Wiki → Query → 新洞见 → 回写 Wiki → 更丰富的 Wiki → 更好的 Query
```

#### 操作三：Lint（健康检查）

定期让 LLM 检查 Wiki 的健康状态：
- 页面之间的**矛盾**
- 被新来源**超越的过时声明**
- 没有入链的**孤儿页面**
- 被提及但缺少独立页面的**重要概念**
- 缺失的**交叉引用**
- 可通过网络搜索填补的**数据空白**

LLM 还擅长建议新的研究问题和需要寻找的新来源。

### 2.3 两个特殊文件

#### index.md——内容目录

面向内容的目录，列出 Wiki 中的每个页面，附链接、一句话摘要和可选元数据。按类别组织（实体、概念、来源等）。LLM 在每次 Ingest 时更新它。

**替代 RAG 的关键机制**：当回答查询时，LLM 先读 index 找到相关页面，再深入阅读。Karpathy 说这在中等规模（约 100 个来源、数百个页面）下效果出奇地好，**完全不需要基于 embedding 的 RAG 基础设施**。

#### log.md——活动日志

按时间顺序的追加式记录，记录发生了什么、何时发生——Ingest、Query、Lint。

实用技巧：每条记录以一致的前缀开头（如 `## [2026-04-02] ingest | 文章标题`），日志就可以用简单的 Unix 工具解析——`grep "^## \[" log.md | tail -5` 获取最近 5 条。

### 2.4 Schema 文件（CLAUDE.md）

这是最关键的配置。它告诉 LLM：

```
# LLM Wiki Schema

## 项目结构
- `raw/` — 不可变的来源文档。绝不修改。
- `wiki/` — LLM 生成的 Wiki。你完全拥有。
- `wiki/index.md` — 主目录。每次 Ingest 更新。
- `wiki/log.md` — 追加式活动日志。

## 页面约定
每个 Wiki 页面必须有 YAML frontmatter：
---
title: 页面标题
type: concept | entity | source-summary | comparison
sources: [引用的 raw/ 文件列表]
related: [链接的 Wiki 页面列表]
created: YYYY-MM-DD
updated: YYYY-MM-DD
confidence: high | medium | low
---

## Ingest 工作流
当我说 "ingest [文件名]"：
1. 读取 raw/ 中的来源文件
2. 与我讨论关键要点
3. 在 wiki/sources/ 创建/更新摘要页
4. 更新 wiki/index.md
5. 更新所有相关的概念和实体页面
6. 在 wiki/log.md 追加一条记录

## Query 工作流
当我提问时：
1. 读 wiki/index.md 找到相关页面
2. 阅读这些页面
3. 综合 answer，附带 [[wiki-link]] 引用
4. 如果回答有价值，提议将其归档为新 Wiki 页面

## Lint 工作流
当我说 "lint"：
1. 检查页面间的矛盾
2. 找出没有入链的孤儿页面
3. 列出被提及但缺少独立页面的概念
4. 检查被新来源超越的过时声明
5. 建议下一步要调查的问题
```

没有 Schema，每次与 LLM 的会话都从零开始。LLM 不知道你的约定、页面格式或工作流。Schema 是**跨会话的持久记忆**，确保一致性。

### 2.5 Idea File 概念

Karpathy 在 Gist 中引入了一个元概念——**Idea File（想法文件）**：

> "在 LLM Agent 时代，分享具体代码/应用的必要性降低了。你只需要分享想法，然后对方的 Agent 会根据你的具体需求定制和构建。"

这个 Gist 本身就是一个 Idea File——它描述模式而非具体实现。Karpathy 说：

> "本文档是故意抽象的。它描述的是想法，不是具体实现。正确的使用方式是把文档分享给你的 LLM Agent，一起实例化一个适合你需求的版本。**文档的唯一职责是传达模式。你的 LLM 能搞定剩下的。**"

这是一种新的"开源"——不是开放代码，而是**开放想法**，由 AI Agent 来解读和实例化。

---

## 3. 设计哲学

### 3.1 "编译一次，保持更新" vs "每次重新推导"

这是整个系统的核心理念：

| 维度 | 传统 RAG | LLM Wiki |
|------|----------|----------|
| 知识处理时机 | 查询时（每次提问） | 摄入时（每个来源一次） |
| 交叉引用 | 每次查询临时发现 | 预先构建并持续维护 |
| 矛盾检测 | 可能不会被发现 | 在 Ingest 时就被标记 |
| 知识积累 | 无——每次查询从零开始 | 随每个来源和问题复利增长 |
| 输出格式 | 聊天回复（短暂的） | 持久的 Markdown 文件（耐久的） |
| 维护者 | 系统（黑盒） | LLM（透明、可编辑） |
| 人的角色 | 上传并查询 | 策展、探索和提问 |

### 3.2 为什么这能工作？

Karpathy 的核心论点：

> "维护知识库的乏味部分不是阅读或思考——而是记账。更新交叉引用、保持摘要最新、标注新数据何时与旧声明矛盾、维护几十个页面之间的一致性。**人类放弃 Wiki 是因为维护负担的增长速度快于价值的增长。** LLM 不会无聊，不会忘记更新交叉引用，并且能一次性触及 15 个文件。Wiki 能保持维护是因为维护成本接近于零。"

### 3.3 Memex 的回响（1945）

Karpathy 在 Gist 最后将这个概念与 Vannevar Bush 1945 年的 **Memex** 联系起来：

> Memex 是一种个人的、策展的知识存储，带有文档之间的关联路径。Bush 的愿景更接近这个，而不是 Web 最终变成的样子：私有的、主动策展的，文档之间的连接和文档本身一样有价值。

Bush 没能解决的问题是：**谁来做维护？** LLM 解决了这个问题。

### 3.4 适用场景

Karpathy 列举了五个具体场景：

| 场景 | 说明 |
|------|------|
| **个人知识库** | 追踪目标、健康、心理学——归档日记条目、文章、播客笔记，随时间构建自我认知的结构化图景 |
| **研究** | 深入一个主题数周或数月——读论文、文章、报告，增量构建带有演进论点的综合 Wiki |
| **读书** | 逐章归档，构建角色、主题、情节线索及其关联的页面。读完后你有一个丰富的伴侣 Wiki |
| **商业/团队** | 由 LLM 维护的内部 Wiki，从 Slack 线程、会议记录、项目文档、客户通话中获取 |
| **其他** | 竞品分析、尽职调查、旅行规划、课程笔记、爱好深潜——任何你在一段时间内积累知识并希望它有条理的场景 |

---

## 4. 评估：能否融入现有工作流

### 4.1 工具依赖

| 组件 | 是否必须 | 替代方案 |
|------|----------|----------|
| **LLM Agent** | **必须** | Claude Code（推荐）、OpenAI Codex、OpenCode 等 |
| **Obsidian** | 推荐，但不必须 | 任何 Markdown 编辑器：VS Code、Typora、Vim |
| **Obsidian Web Clipper** | 推荐（网络来源） | 手动保存为 Markdown、浏览器打印为 PDF |
| **Git** | 推荐 | 不用也行，但失去版本历史 |
| **qmd（搜索引擎）** | 可选（大规模时） | 小规模用 index.md 就够了 |
| **Marp（幻灯片）** | 可选 | 不需要 |
| **Dataview** | 可选 | 不需要 |

**核心依赖实际上只有一个：一个能读写本地文件的 LLM Agent。** 其他都是锦上添花。

### 4.2 工具链与你现有环境的匹配度

根据你当前的工作环境（Windows + VS Code + Claude Code + Git）：

| 你已有的 | LLM Wiki 需要的 | 匹配度 |
|----------|-----------------|--------|
| VS Code | 任何 Markdown 编辑器 | ✅ 完全匹配 |
| Claude Code | LLM Agent | ✅ 完全匹配 |
| Git | 版本控制 | ✅ 完全匹配 |
| Markdown 文件 | 所有内容都是 Markdown | ✅ 完全匹配 |

**好消息：你现有工具链几乎无缝对接 LLM Wiki 模式。** 不需要安装新软件（除非你想用 Obsidian 的图谱视图等高级功能）。

### 4.3 对现有工作习惯的侵入性

| 方面 | 影响程度 | 说明 |
|------|----------|------|
| **文件管理** | 低 | 只是在项目中增加 `raw/` 和 `wiki/` 两个目录 |
| **日常工作流** | 中 | 你需要主动往 `raw/` 丢资料，并告诉 LLM 处理。这是一套新的工作习惯 |
| **知识获取方式** | 高 | 从"自己翻笔记"变为"问 Wiki"。需要一个适应期 |
| **与现有笔记系统的关系** | 取决于你是否迁移 | 如果你已经在用 Notion/Obsidian 做笔记，需要决定是迁移还是并行 |

### 4.4 最大的工作流障碍

1. **习惯养成**：需要持续地往 `raw/` 丢资料，而不是顺手丢进笔记软件或收藏夹。这是一道习惯门槛
2. **"冷启动"阶段**：Wiki 的价值随来源数量增长。10 个来源以内，你可能觉得不如直接搜。需要坚持到一定规模才能感受到复利效果
3. **Claude Code 的使用方式改变**：你需要在 Claude Code 中切换思维模式——从"帮我写代码"变为"帮我维护知识库"

---

## 5. 评估：长期维护成本与可靠性

### 5.1 知识库膨胀问题

**规模增长的挑战**：

| Wiki 规模 | 状态 | 推荐策略 |
|-----------|------|----------|
| < 50 个来源 | index.md 完全够用 | 无需额外工具 |
| 50-200 个来源 | index.md 开始吃力 | 考虑引入 qmd 搜索 |
| 200+ 个来源 | index.md 可能超出上下文窗口 | 必须用 qmd 或类似工具 |
| 500+ 个来源 | 大规模知识库 | 需要分区策略 + 搜索引擎 |

Karpathy 自己的研究 Wiki：约 100 篇文章，约 40 万字，涵盖单一 ML 研究主题。在这个规模下 index.md 方案仍然有效。

### 5.2 准确性风险

**LLM 生成的笔记质量可靠吗？** 这是核心担忧。

| 风险 | 严重程度 | 缓解机制 |
|------|----------|----------|
| **幻觉（Hallucination）** | 中 | Raw 不可变，可随时回溯验证。Schema 要求标注 confidence 等级 |
| **摘要失真** | 中 | Lint 操作会检查矛盾。但 Lint 本身也是 LLM 执行的 |
| **逐步偏离** | 高 | 随着多次 Ingest，LLM 可能在摘要的摘要上再做摘要，误差累积 |
| **来源覆盖不全** | 低 | Lint 会检查孤儿页面和缺失概念 |

**最根本的保障是 Raw 层的不可变性。** 如果你怀疑 Wiki 中的某个声明，总能回到 Raw 去核实。这比 RAG 的黑盒检索透明得多。

### 5.3 维护负担

**谁来维护？**

| 维护任务 | 执行者 | 频率 |
|----------|--------|------|
| Ingest 新资料 | 你 + LLM | 每次有新资料时 |
| 日常 Query | 你 + LLM | 按需 |
| Lint 健康检查 | LLM | 建议每周一次 |
| Schema 演化 | 你 + LLM | 渐进式，随使用发现需要调整 |
| index.md 更新 | LLM | 每次 Ingest 自动 |
| log.md 追加 | LLM | 每次 Ingest/Query/Lint 自动 |

**你的实际维护负担**：策展资料（决定什么值得收入 Wiki）+ 偶尔检查 LLM 的输出质量。其余全是 LLM 的事。

### 5.4 一个被忽略的风险：幻觉累积

Gist 讨论区中有人提出了一个尖锐的问题：

> "如果每条 AI 生成的笔记都有非零的幻觉概率，那么在重复使用下，Wiki 中至少包含一条幻觉笔记的概率趋近于 1。"

缓解方案（来自社区）：
- **Socratic 模式**：不让 AI 直接写 Wiki，而是你先提出观点，AI 作为质疑者挑战和完善，只有经过这个过程才写入
- **来源溯源**：每个声明追溯到 Raw 中的具体位置
- **置信度分级**：在 frontmatter 中标注 confidence，优先审查低置信度页面

### 5.5 Git 作为安全网

整个 Wiki 就是 Git 仓库里的 Markdown 文件，你天然拥有：
- `git log` 查看 Wiki 的演化历史
- `git diff` 查看每次 Ingest 具体改了什么
- `git revert` 回滚一次糟糕的"编译"
- `git blame` 追溯某个声明是何时加入的

**这比任何笔记软件的撤销功能都更可靠。**

---

## 6. 结论

### 6.1 LLM Wiki 适合你吗？

**适合的人**：
- 长期深入研究某个主题（研究者、学生）
- 愿意投入初期时间建立习惯，等待复利效果
- 已经在日常使用 LLM Agent（特别是 Claude Code）
- 有大量需要结构化组织的文字资料
- 接受"LLM 做苦力、我做策展"的分工模式

**不适合的人**：
- 知识需求是短期的、一次性的
- 不信任 LLM 生成的摘要，需要每条都自己验证
- 资料量很小（少于 20 个来源），直接读比建 Wiki 更快
- 不想改变现有笔记习惯

### 6.2 与你的 Writer 项目的关联

你正在做的 Writer 项目（AI 辅助写作工具）中，storybuilding 模块已经在处理"持久化的结构化知识"（设定集、角色卡、世界观）。LLM Wiki 的思路与 Writer 有理念上的共鸣——都是让 LLM 维护结构化的知识体系。

具体可能的启发：
- Writer 的 `.webnovel/` 目录结构可以参考 LLM Wiki 的 `raw/ → wiki/` 分层
- 设定集的一致性检查类似 LLM Wiki 的 Lint 操作
- "知识编译一次而非每次重查"的思路，可以应用于写作中的上下文管理

### 6.3 个人判断

LLM Wiki 是一个**思路优雅、实际门槛低**的知识管理模式。它的核心洞察——"编译知识而非每次重新检索"——是反直觉但正确的。

但它的价值严重依赖**使用规模**。如果你不会持续积累超过 50 个来源在同一个主题上，它的优势不如直接用 Obsidian + 全文搜索。

**建议**：如果你有一个即将深入研究 2+ 周的主题，可以试着用这个模式。从 10 个来源开始，看看 Wiki 给你的洞察是否比你单独阅读这些来源更深刻。如果是，说明这个模式对你有效，继续投入。

---

## 7. 实操指南：从零搭建你的 LLM Wiki

这一章回答一个具体问题：**如果我今天就要用 LLM Wiki，具体怎么操作？**

以下假设你用 **Claude Code**（你已经在用）。如果你用 Codex，把 `CLAUDE.md` 替换为 `AGENTS.md` 即可。

### 7.1 第一步：创建项目目录

选一个你要研究的主题，创建项目根目录。以"学习 Transformer 架构演进"为例：

```bash
mkdir -p transformer-wiki/raw/articles
mkdir -p transformer-wiki/raw/papers
mkdir -p transformer-wiki/raw/assets
mkdir -p transformer-wiki/wiki/concepts
mkdir -p transformer-wiki/wiki/entities
mkdir -p transformer-wiki/wiki/sources
mkdir -p transformer-wiki/wiki/comparisons

# 初始化 Git（强烈建议）
cd transformer-wiki
git init
```

此时目录结构：

```
transformer-wiki/
├── .git/
├── raw/
│   ├── articles/
│   ├── papers/
│   └── assets/
└── wiki/
    ├── concepts/
    ├── entities/
    ├── sources/
    └── comparisons/
```

### 7.2 第二步：创建 Schema 文件（CLAUDE.md）

在项目根目录创建 `CLAUDE.md`。**这是最重要的一步。** 以下是一个可直接使用的模板：

```markdown
# LLM Wiki Schema

## 角色
你是一个知识库维护者。你的职责是根据 raw/ 中的原始资料，构建和维护 wiki/ 中的结构化知识库。

## 项目结构
- `raw/` — 不可变的来源文档。你只读，绝不写入。
- `wiki/` — 你完全拥有的知识库。你创建和更新所有内容。
- `wiki/index.md` — 主目录。每次 Ingest 后必须更新。
- `wiki/log.md` — 追加式活动日志。每次操作后追加记录。
- `wiki/overview.md` — 高层综述。定期更新。

## 页面约定
每个 wiki 页面必须有 YAML frontmatter：

---
title: 页面标题
type: concept | entity | source-summary | comparison
sources: [引用的 raw/ 文件列表]
related: [链接的 wiki 页面列表]
created: YYYY-MM-DD
updated: YYYY-MM-DD
confidence: high | medium | low
---

## Ingest 工作流
当我说 "ingest [文件名]" 或 "ingest raw/ 中的新文件"：
1. 读取 raw/ 中指定的来源文件
2. 向我概述关键要点（3-5 条）
3. 等我确认后再继续
4. 在 wiki/sources/ 创建摘要页
5. 检查是否需要创建新的概念页或实体页
6. 更新所有已有的相关页面（概念、实体、对比）
7. 更新 wiki/index.md
8. 在 wiki/log.md 追加记录（格式：## [日期] ingest | 标题）

## Query 工作流
当我提问时：
1. 先读 wiki/index.md 找到相关页面
2. 阅读那些页面
3. 综合回答，用 [[page-name]] 格式引用 Wiki 页面
4. 如果回答有长期价值，提议归档为新 Wiki 页面

## Lint 工作流
当我说 "lint"：
1. 检查页面间的矛盾
2. 找出没有入链的孤儿页面
3. 列出被提及但缺少独立页面的概念
4. 检查被新来源超越的过时声明
5. 建议下一步要调查的问题
6. 输出结构化的健康报告
```

**注意**：这是一个起点。用了一两周后，你会发现需要调整——可能要加新的页面类型、修改 Ingest 流程、或者加特殊情况处理。这就是 Karpathy 说的"你和 LLM 共同演化 Schema"。

### 7.3 第三步：放入第一批资料

往 `raw/` 中放入你的第一批来源。方法取决于资料类型：

**网页文章**：
- 用 Obsidian Web Clipper 浏览器扩展一键剪藏为 Markdown
- 或手动复制粘贴为 `.md` 文件
- 存到 `raw/articles/`

**论文 PDF**：
- 直接下载 PDF 放到 `raw/papers/`
- Claude Code 可以直接读 PDF

**你已有的笔记/文档**：
- 直接复制到 `raw/articles/` 或 `raw/papers/`
- 加上 YAML frontmatter（来源、日期等）

**建议**：第一批放 **3-5 个来源**，不要太多。少而精地走通整个流程，感受每个步骤。

### 7.4 第四步：Ingest 第一篇资料

在项目根目录打开 Claude Code：

```bash
cd transformer-wiki
claude
```

然后对 Claude Code 说：

```
ingest raw/articles/2026-04-attention-revisited.md
```

Claude Code 会：
1. 读 `CLAUDE.md` 了解它是"Wiki 维护者"
2. 读你指定的 raw 文件
3. 向你概述关键要点（你在 Schema 中要求的步骤 2）
4. **等你确认**（这一步很关键——你要参与把控质量）
5. 你确认后，它创建 wiki 页面、更新 index.md、追加 log.md

你的角色：**读摘要、纠正偏差、引导侧重点**。你说"这点很重要，多展开"或"这里理解错了，应该是 XXX"。

完成后提交：

```bash
git add . && git commit -m "ingest: Attention is All You Need Revisited"
```

### 7.5 第五步：重复 Ingest，感受 Wiki 增长

继续 Ingest 第二篇、第三篇……每次都会看到：
- `wiki/sources/` 多了一篇摘要
- `wiki/concepts/` 可能出现了新的概念页（如果文章提到了之前没有的概念）
- `wiki/entities/` 可能出现了新的实体页
- 已有的概念页和实体页被更新了——**这就是复利效果**
- `wiki/index.md` 越来越丰富
- `wiki/log.md` 成了一条时间线

**关键检查点：Ingest 5-10 个来源后**，试着提一个需要综合多篇文章的问题：

```
综合目前 Wiki 中的所有来源，Transformer 架构从 2017 到 2026 
最重要的三个演进方向是什么？每个方向的关键论文和发现是什么？
```

如果 Wiki 给你的回答比你自己翻这几篇文章更清晰、更有结构感——说明系统在工作。

### 7.6 第六步：Query 和回写

日常使用中，你不需要每次都 Ingest。更多时候是在**提问**：

```
# 问事实
Wiki 中关于 MoE 路由策略的结论是什么？

# 问对比
对比 Flash Attention 1 和 2 的核心差异。

# 问综合
基于目前所有来源，scaling law 在 2026 年的最新共识是什么？

# 问开放探索
目前 Wiki 中还有哪些矛盾或未解答的问题？
```

**回写**：当一个 Query 的回答特别好——比如一次精彩的综合分析——告诉 Claude Code：

```
把这个分析归档为 wiki 页面。
```

这会让你的**探索也变成 Wiki 的一部分**，而不仅仅是来源。

### 7.7 第七步：定期 Lint

每周（或每 Ingest 10 个来源后）执行一次 Lint：

```
lint
```

Claude Code 会输出类似这样的健康报告：

```
Wiki 健康报告 (2026-06-11)

矛盾（2 处）：
- concepts/dense-vs-sparse.md 声称 <10B 参数时 dense 更优，
  但 sources/summary-moe-efficiency.md 显示相反
- entities/openai.md 写 GPT-5 是 200B 参数，
  但 sources/summary-gpt5-leak.md 说是 300B

孤儿页面（3 个）：
- concepts/tokenization.md（没有入链）
- sources/summary-old-bert-paper.md（没有被引用）

缺失页面（建议创建）：
- "RLHF" 被提及 12 次但没有独立页面
- "KV Cache" 在 5 个来源中被引用但没有页面
```

你可以决定怎么处理——让 LLM 修复矛盾、创建缺失页面、或者标记为"待研究"。

### 7.8 典型的一天：日常使用模式

```
上午：浏览 RSS / Twitter / 论文列表
  └─ 看到一篇好文章 → 用 Web Clipper 剪藏到 raw/articles/

中午：
  └─ 打开 Claude Code → "ingest raw/articles/最新剪藏的文件.md"
  └─ 读 LLM 的摘要，确认或纠正
  └─ git commit

下午：工作中遇到问题
  └─ 打开 Claude Code → 提问
  └─ 得到基于 Wiki 的回答（不是通用回答，而是基于你读过的所有资料）

晚上：
  └─ "lint" → 看看 Wiki 健康状况
  └─ 处理报告中的问题
  └─ git commit
```

### 7.9 常见问题

**Q：我应该在每个 Claude Code 会话开头说什么？**

不需要特别说什么。Claude Code 会自动读 `CLAUDE.md`（Schema）。它知道自己的角色。直接说 `ingest xxx` 或提问即可。

**Q：我可以直接编辑 Wiki 中的文件吗？**

可以，但不建议频繁这样做。如果你手动改了某个页面，LLM 不知道你改了什么，后续 Ingest 可能覆盖你的修改或产生不一致。**更好的做法**是通过对话让 LLM 做修改。

**Q：如果 LLM 的摘要写得不好怎么办？**

在 Ingest 步骤 2（"概述关键要点"）时纠正它。你说"第三点理解错了，应该是 XXX"，LLM 会按你的纠正来写 Wiki 页面。这就是为什么 Karpathy 推荐逐个 Ingest 并保持参与。

**Q：Wiki 长到什么规模需要引入 qmd 搜索？**

Karpathy 的经验是：约 100 个来源、数百个 Wiki 页面时，index.md 还是够用的。超过这个规模，考虑安装 qmd（Shopify CEO Tobi Lutke 做的本地 Markdown 搜索引擎），提供 BM25 + 向量混合搜索。

```bash
npm install -g @tobilu/qmd
qmd collection add ./wiki --name my-research
qmd query "mixture of experts routing"  # 混合搜索
```

**Q：我需要 Obsidian 吗？**

不是必须的。VS Code 就能浏览 Markdown 文件。但 Obsidian 的**图谱视图**（Graph View）能可视化 Wiki 页面之间的 `[[双链]]` 关系，帮你直观地看到哪些页面是枢纽、哪些是孤儿。如果你喜欢可视化，值得装一个。

**Q：多个主题怎么办——一个 Wiki 还是多个？**

取决于主题之间的关联度：
- 如果两个主题高度相关（如 "Transformer 架构" 和 "大模型训练技术"）→ 放在同一个 Wiki
- 如果完全不相关（如 "Transformer 架构" 和 "日本料理"）→ 分开建不同的 Wiki（不同的项目目录）

---

## 参考资料

- [Karpathy 原始 Gist：LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)（2026-04-04）
- [Agentpedia: Karpathy's LLM Wiki Complete Guide](https://agentpedia.codes/blog/karpathy-llm-wiki-idea-file)
- [MindStudio: How to Set Up Karpathy's LLM Wiki](https://www.mindstudio.ai/blog/andrej-karpathy-llm-wiki-knowledge-base-claude-code/)
- [Analytics Vidhya: LLM Wiki Revolution](https://www.analyticsvidhya.com/blog/2026/04/llm-wiki-by-andrej-karpathy/)
- [Vannevar Bush: "As We May Think"](https://www.theatlantic.com/magazine/archive/1945/07/as-we-may-think/303881/)（1945，原始 Memex 论文）
