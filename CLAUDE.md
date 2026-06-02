# CLAUDE.md

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

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

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



## Skills Trigger Rules

This project uses a state-machine workflow for complex tasks. When user intent matches, **MUST** invoke the corresponding Skill immediately:

| Intent | Keywords | Skill | Goal |
|--------|----------|-------|------|
| Requirements analysis | "需求分析", "评估可行性" | `/require` | Deep sandbox analysis before coding |
| Implementation planning | "制定策略", "出个方案", "步骤拆解" | `/planning` | Multi-round architecture decision |
| Production coding | "规范写代码", "按计划开发" | `/coding` | Principal Engineer mode — no glue code |

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

## 项目结构速览

目标：让模型先理解”请求从前端进入 -> 后端 API -> DeepAgents 主代理/子代理 -> 本地工作区持久化 -> 前端展示”的主链路，只关注核心代码。

```text
Writer/
├─ docs/                             # 规格文档
│  ├─ character-profile-spec.md      # 角色档案规格
│  ├─ outline-structure-spec.md      # 大纲结构规格
│  └─ detailed-outline-spec.md       # 详细大纲规格
├─ backend/                          # Python 后端：FastAPI + DeepAgents 写作服务
│  ├─ pyproject.toml                 # 后端依赖与 Python 版本；核心依赖 deepagents/fastapi/uvicorn
│  ├─ workspace/                     # 运行时数据目录；JSON 持久化 + 各工作区生成产物
│  └─ app/
│     ├─ main.py                     # API 入口；注册 workspace/thread/generate/trace/export/style 等路由
│     ├─ core/                       # 后端基础设施
│     │  ├─ settings.py              # 读取环境变量；控制模型、模式、CORS 等运行配置
│     │  ├─ thread_store.py          # 本地持久化；管理 workspace、thread、outline、character、novel
│     │  └─ style_store.py           # 风格配置持久化；管理写作风格的增删改查
│     ├─ schemas/                    # FastAPI/Pydantic 数据契约
│     │  ├─ screenplay.py            # 工作区、线程、剧本/小说生成相关请求响应模型
│     │  ├─ character.py             # 角色生成请求响应模型
│     │  └─ style.py                 # 风格相关请求响应模型
│     ├─ create_type/                # 创作类型模块
│     │  ├─ schemas.py               # 创作类型数据模型
│     │  ├─ store.py                 # 创作类型持久化
│     │  ├─ router.py                # 创作类型 API 路由
│     │  └─ optimizer.py             # 创作类型优化器
│     └─ writer/                     # DeepAgents 写作核心
│        ├─ meta_agent.py            # 主代理服务；编排大纲、正文、长任务流式生成和子代理调用
│        ├─ models.py                # 写作域模型；供代理和服务层共享
│        ├─ deepseek_thinking.py     # DeepSeek 思考集成；扩展推理能力
│        ├─ prompt/                  # 主代理系统提示词
│        │  └─ meta_agent_system_prompt.txt
│        ├─ subagents/               # 专业子代理（每个子代理含代码 + prompt/ 目录）
│        │  ├─ outline/              # 大纲/结构规划
│        │  │  ├─ outline_subagent.py
│        │  │  └─ prompt/outline_system_prompt.txt
│        │  ├─ detail_outline/       # 详细大纲生成
│        │  │  ├─ detail_outline_subagent.py
│        │  │  └─ prompt/detail_outline_system_prompt.txt
│        │  ├─ writing/              # 正文写作
│        │  │  ├─ writing_subagent.py
│        │  │  └─ prompt/writing_system_prompt.txt
│        │  ├─ evaluation/           # 质量评估
│        │  │  ├─ evaluation_subagent.py
│        │  │  └─ prompt/            # 含 outline/detail_outline/review 三类评估提示词
│        │  └─ character/            # 角色设定生成
│        │     ├─ character_subagent.py
│        │     └─ prompt/character_system_prompt.txt
│        ├─ middleware/              # DeepAgents 执行中间件
│        │  ├─ trace_middleware.py   # 记录代理执行过程
│        │  ├─ trace_callback.py     # 将回调事件写入 trace
│        │  ├─ path_guard_middleware.py  # 限制代理文件访问范围
│        │  ├─ goal_middleware.py    # 注入/维护写作目标上下文
│        │  ├─ error_recovery_middleware.py  # 错误恢复与重试
│        │  ├─ artifact_prerequisite_middleware.py  # 产物前置条件检查
│        │  └─ context_assembler_middleware.py  # 上下文组装
│        ├─ tools/                   # 代理可调用工具
│        │  └─ goal.py               # 写作目标相关工具
│        └─ trace/                   # 可观察性链路
│           ├─ schemas.py            # trace 事件、摘要、详情模型
│           ├─ recorder.py           # trace 读写与删除
│           └─ projector.py          # 将底层事件投影成前端展示结构
├─ frontend/                         # Next.js 前端：写作工作台 UI
│  ├─ package.json                   # 前端依赖与脚本；dev 默认端口 3456
│  ├─ app/
│  │  ├─ layout.tsx                  # App Router 根布局
│  │  ├─ page.tsx                    # 主工作台；管理工作区、会话、生成流、面板状态
│  │  ├─ globals.css                 # 全局样式
│  │  └─ generated/                  # 生成内容输出目录
│  ├─ components/workspace/          # 工作台组件
│  │  ├─ AppShell.tsx                # 页面整体布局骨架
│  │  ├─ Sidebar.tsx                 # 工作区/会话列表与切换
│  │  ├─ TopBar.tsx                  # 顶部上下文和操作区
│  │  ├─ ChatPanel.tsx               # 用户输入、消息流、生成状态
│  │  ├─ ScriptPanel.tsx             # 大纲/剧本结果展示
│  │  ├─ DetailOutlinePanel.tsx      # 详细大纲结果展示
│  │  ├─ CharactersPanel.tsx         # 角色结果展示
│  │  ├─ NovelPanel.tsx              # 小说正文展示与 PDF 导出入口
│  │  ├─ StyleModal.tsx              # 风格配置弹窗
│  │  ├─ TracePanel.tsx              # 代理 trace 详情展示
│  │  ├─ ToolTree.tsx                # 工具/子代理调用树展示
│  │  ├─ SessionMenu.tsx             # 会话操作菜单
│  │  └─ ConfirmDialog.tsx           # 删除等危险操作确认框
│  └─ lib/                           # 前端非 UI 逻辑
│     ├─ api.ts                      # 后端 API 封装；含 SSE 解析和 PDF URL 构造
│     ├─ types.ts                    # 前端共享类型
│     ├─ outline.ts                  # 大纲内容处理工具
│     └─ trace.ts                    # trace 数据处理工具
├─ start-dev.ps1 / start-dev.cmd      # 一键启动后端和前端开发服务
└─ README.md                          # 项目定位、运行方式和环境变量说明
```

核心理解路径：先读 `frontend/app/page.tsx` 看用户操作和状态流，再读 `frontend/lib/api.ts` 看请求契约，然后读 `backend/app/main.py` 看 API 路由如何调用 `MetaAgentService`、`CharacterService`、`ThreadStore` 和 `StyleStore`，最后读 `backend/app/writer/` 理解 DeepAgents 的主代理、子代理（每个子代理含独立代码 + prompt）、middleware、trace 如何协作。
