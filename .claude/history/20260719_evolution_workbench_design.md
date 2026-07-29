---
type: design
status: draft
created: 2026-07-19 00:00
require: 20260719_evolution_agent_refactor.md
related:
  - evolution/app/evolve/agent/agent.py
  - evolution/app/evolve/agent/prompt.py
  - evolution/app/evolve/api.py
  - evolution/app/evolve/agent/flow_guard.py
  - evolution/desktop/src/pages/evolve.tsx
  - evolution/desktop/src/pages/review-report.tsx
  - executor/app/platform/agent/runtime/factory.py
  - executor/app/platform/core/checkpoint_pool.py
  - executor/app/platform/streaming/event_stream.py
---

# 进化Agent对话式共创工作台 — 设计方案

## 0. 设计目标（来自需求基准）

把"启动→全自动跑完→pending_review→二选一发布"重构为"对话式共创→拍板→落地→review-report"。
后端：DeepAgents + LangGraph 多轮对话原语。前端：Tauri+React 双Tab工作台。

## 1. 技术可行性盘点结论（来自双探索）

### 1.1 后端可行性：高
- DeepAgents 透传 LangGraph 全部能力：State / interrupt / checkpointer / store / astream_events
- writer 端有生产验证的同款实现：`thread_id` + `AsyncSqliteSaver` + `Command(resume=...)` + WritingEventSink
- 进化端目前 `checkpointer=None`、单次 ainvoke、无 thread_id——改造空间是"从 0 到 1"
- **最优路径已定**：路径3 = checkpointer + thread_id + 多次 ainvoke（不动 DeepAgents 框架）

### 1.2 前端可行性：高
- 60% 可复用（CSS 全套、流式基础设施、抽屉范式、markdown 渲染、API 中继）
- 40% 新建：EvolveChatPanel / EvolveProposalCard / EvolveMarkdown / EvolveDrawer / useEvolveWorkbenchStore

### 1.3 最大缺口（需后端配合）
- SSE 现仅 `start/log/step/heartbeat/end/error`，缺 `assistant_message/model_stream/proposal/finalizing/ask_user`
- agent.py 单次 ainvoke，无对话循环
- 缺 `POST /messages`、`GET /proposals`、`POST /finalize` 端点

## 2. 关键设计断层（已炸干净）

### 断层 1：探查阶段呈现 = B 可见事件流 + 折叠
**决策**：探查阶段对用户完全可见（emit_step/emit_log 复用），完成后这些事件折叠为一行"已读取 N 条证据 + 探查 N 个要素"，然后 Agent 发开场白。透明、可追责、不堆屏。

### 断层 2：拍板按钮启用条件 = A ≥1 个 accepted 即可
**决策**：用户有绝对控制权，至少 1 个 accepted 进化点即可拍板，允许 reject/ignore 其他点。

---

## 3. 已确认的关键技术决策

### 决策 T1：对话循环走 LangGraph 原生多轮对话（路径3）
- 给 `create_deep_agent` 传 `checkpointer=AsyncSqliteSaver`（per-session 一个 db 或共用池）
- 每个 session 一个 `thread_id = session_id`
- 每轮用户消息：`await agent.ainvoke({"messages":[{"role":"user","content":msg}]}, config={"configurable":{"thread_id": session_id}})`
- LangGraph 自动从 checkpoint 取历史 messages，第 N 轮 Agent 看到完整对话史
- **不动 DeepAgents 框架**，所有改造在 `evolution/app/evolve/` 内
- 参考 writer 端 `executor/app/platform/agent/runtime/` + `core/checkpoint_pool.py` 的生产范本

### 决策 T2：按需触发模型（无状态 runners）
- session 不常驻 task。用户发一条消息 → POST `/messages` → 启动后台 task 跑一轮 → 跑完 task 结束
- 状态全在 LangGraph checkpoint + DB，runner 进程无状态
- stop = 取消当前 task（不会丢对话状态，checkpoint 已落库）
- 进程重启无影响，下次消息自然恢复
- 与现有 `_run_evolve_bg` 模式一致，扩展而非重写

### 决策 T3：进化点纯 DB 存储，拍板时生成 design_doc.md
- `evolve_points` 表是唯一真相源
- Agent 调 propose/update/reject → 写 DB（通过 evolve context 注入的 repo）
- 拍板瞬间从 accepted 进化点导出 design_doc.md（供 review-report/publish_session/registry 使用）
- 不再用 write_design_doc 工具，design_doc 不再是 Agent 直接产物

### 决策 T5：进化端独立 checkpointer
- 路径：`evolution/data/checkpoints/evolve_<session_id>.db`
- per-session 一个 SQLite 文件，discarded session 直接删文件清理
- 新建 `evolution/app/evolve/agent/checkpoint_pool.py`，参考 `executor/app/platform/core/checkpoint_pool.py` per-user 模式改为 per-session

### 决策 T6：消息表 evolve_messages（统一表 + role 区分）
```sql
evolve_messages:
  id              TEXT PRIMARY KEY      -- uuid
  session_id      TEXT NOT NULL         -- FK evolve_sessions
  role            TEXT NOT NULL         -- user / assistant / system / tool
  content         TEXT NOT NULL         -- 消息正文（markdown）
  tool_events     JSON                  -- 该消息触发的工具调用列表（assistant 消息专属）
  related_points  JSON                  -- 该消息涉及的进化点 id 列表（用于浮窗联动高亮）
  seq             INTEGER NOT NULL      -- 会话内序号
  created_at      TEXT NOT NULL
  UNIQUE(session_id, seq)
```

### 决策 T7：进化点表 evolve_points（完整结构表）
```sql
evolve_points:
  id              TEXT PRIMARY KEY      -- uuid，Agent 调 propose 时生成
  session_id      TEXT NOT NULL
  seq             INTEGER NOT NULL      -- 会话内序号（浮窗排序）
  target          TEXT NOT NULL         -- 要改的要素（meta_system.md / RetryMiddleware 等）
  problem         TEXT NOT NULL         -- 为什么改（含 finding 引用）
  options         JSON NOT NULL         -- [{description, pros, cons, expected_impact}, ...]
  recommendation  TEXT                  -- 推荐哪个 option + 理由
  note            TEXT                  -- Agent 补充说明
  status          TEXT NOT NULL         -- proposed / accepted / rejected
  chosen_option   INTEGER               -- 用户选了第几个 option（accepted 时）
  user_note       TEXT                  -- 用户附加说明
  accepted_at     TEXT                  -- accept/reject 时间
  design_ref      INTEGER               -- 拍板后映射到 design_doc 的 change 序号
  created_at      TEXT NOT NULL
```

### 决策 T8：prompt.py 函数级分离
- 保留 `evolve_system_prompt(...)` 入口（动态注入不变）
- 内部拆为：`STATIC_BLUEPRINT`（模块级常量字符串）+ `_inject_dynamic(...)`（动态部分拼接到 STATIC_BLUEPRINT 后）
- 蓝图 API `GET /api/evolve/system-prompt` 返回 `STATIC_BLUEPRINT` 字符串
- 前端 react-markdown 渲染（标题/列表/表格/代码块自然结构化）

### 决策 T9：FlowGuard 阶段门控（基于 session.status）
- 现有 `FlowGuardMiddleware` 改造：读 `EvolveContext` 的当前 session.status
- conversing 阶段：拦截 `edit_source / write_* / validate_changes / write_design_doc / write_change_log`
- finalizing 阶段：解锁全部工具
- 复用现有架构，最小改动

---

## 4. 架构快照（整合所有决策）

### 4.1 整体数据流

```
[前端 /evolve 双Tab]
  ├─ Tab1「架构蓝图」─────GET /api/evolve/system-prompt──────────────→ STATIC_BLUEPRINT
  └─ Tab2「进化工作台」
       ├─ 启动会话 ──────POST /api/evolve/start──────────────────→ 创建 session, status=running
       │                  ↓ 后台 task: Agent 探查阶段
       │                  ↓ EvolveEventSink → SSE 流
       │                  ↓ 探查完 → Agent 发开场白 → status=conversing
       │
       ├─ 中部对话区
       │   ├─ 发消息 ────POST /api/evolve/sessions/{id}/messages──→ 后台 task 跑一轮 astream
       │   ├─ 停止 ──────POST /api/evolve/sessions/{id}/stop──────→ 取消当前 task
       │   └─ 拍板 ──────POST /api/evolve/sessions/{id}/finalize─→ status=finalizing
       │                  ↓ 后台 task: Agent 落地阶段
       │                  ↓ 成功 → pending_review → 前端 navigate review-report
       │
       └─ 右侧浮窗 ────GET /api/evolve/sessions/{id}/points──────→ 进化点清单（含 status）
                        ↓ SSE proposal 事件实时同步状态

[review-report 页（保留）]
  └─ GET /api/evolve/sessions/{id} → 含 design_doc.md + change_log.md
      ├─ POST .../publish → publish_session（不变）
      └─ POST .../discard → discard_session（不变 + 删 checkpoint db）
```

### 4.2 状态机（完整版）

```
[创建] → running（探查阶段，Agent 读评估+探查要素）
              │
              ↓ Agent 发开场白
         conversing（对话共创，按需触发多轮）
              │
              ├─ 用户发消息 → conversing（循环）
              │
              ↓ 用户点拍板（POST /finalize）
         finalizing（落地阶段，一次性）
              │
              ├─ 成功 → pending_review → published 或 discarded
              │
              └─ 失败 → failed → discarded（清理 git working tree + checkpoint）
```

**状态转换触发**：
- running → conversing：Agent 探查完 + 发开场白（自动）
- conversing → conversing：用户发消息（POST /messages）
- conversing → finalizing：用户点拍板（POST /finalize）
- finalizing → pending_review：Agent 落地成功
- finalizing → failed：Agent 落地失败
- pending_review → published：用户在 review-report 点发布
- pending_review → discarded：用户在 review-report 点丢弃
- failed → discarded：用户在工作台点丢弃（清理）

### 4.3 后端模块改造图

```
evolution/app/evolve/
├─ agent/
│   ├─ agent.py           ★ 改造：build_evolve_agent 加 checkpointer；run_evolve_session 拆为
│   │                              _run_inspect_round（探查）+ _run_converse_round（对话）+ _run_finalize_round（落地）
│   ├─ prompt.py          ★ 改造：拆 STATIC_BLUEPRINT + _inject_dynamic
│   ├─ flow_guard.py      ★ 改造：读 session.status 拦截落地工具（conversing 阶段）
│   ├─ tools.py           ★ 改造：新增 propose_evolution_point / update_evolution_point /
│   │                              reject_evolution_point / finalize_evolution_plan 工具
│   ├─ event_sink.py      ★ 新建：EvolveEventSink，监听 astream_events 转 SSE
│   ├─ checkpoint_pool.py ★ 新建：per-session AsyncSqliteSaver 池
│   └─ evolve_repo.py     ★ 新建：EvolvePointsRepo + EvolveMessagesRepo（DB 访问层）
├─ api.py                 ★ 改造：新增端点（见 § 5）
├─ ctx.py                 ★ 改造：EvolveContext 增加 points_repo / messages_repo / session_status
├─ db.py                  ★ 改造：新增 evolve_messages / evolve_points 表 + 迁移
└─ docs.py                ★ 改造：新增 generate_design_doc_from_points(session_id)
```

### 4.4 前端组件结构

```
evolution/desktop/src/
├─ pages/
│   └─ evolve.tsx         ★ 重写：双 Tab 容器
├─ components/
│   └─ evolve/            ★ 新建目录
│       ├─ BlueprintTab.tsx       ★ 新建：架构蓝图 Tab（react-markdown 渲染 STATIC_BLUEPRINT）
│       ├─ WorkbenchTab.tsx       ★ 新建：进化工作台 Tab（layout 容器）
│       ├─ EvolveChatPanel.tsx    ★ 新建：参考 ChatPanel，去创作端耦合
│       ├─ EvolveMessage.tsx      ★ 新建：单条消息渲染（含 markdown + 折叠引用 + 进化点卡片）
│       ├─ EvolveMarkdown.tsx     ★ 新建：react-markdown + 自定义渲染 [[finding:xx]] / [[propose:xx]]
│       ├─ EvolveProposalCard.tsx ★ 新建：参考 InterviewOptions，propose 卡片渲染
│       ├─ EvolveDrawer.tsx       ★ 新建：参考 TraceChainDrawer，浮窗（进化点清单 + 拍板按钮）
│       ├─ EvolveComposer.tsx     ★ 新建：输入框（参考 chat-composer CSS）
│       ├─ ProgressTrack.tsx      ★ 新建：探查/落地阶段的进度事件流（折叠展示）
│       └─ SessionList.tsx        ★ 改造：从 evolve.tsx 抽出，保留历史会话列表
├─ stores/
│   └─ evolveWorkbench.ts ★ 新建：zustand store（消息状态机 + 流式拼接 + 进化点列表）
├─ lib/
│   ├─ api.ts             ★ 改造：新增 evolve 对话相关 API
│   └─ stream.ts          ◐ 保留：evoSseStream 已支持，扩展 frame.type 分支即可
└─ styles/globals.css     ★ 改造：清理进化相关 CSS 暗色残留；新增 .evolve-workbench / .evolve-proposal / .evolve-drawer 等样式
```

---

## 5. API 契约（新增 + 改造端点）

### 5.1 新增端点

#### `GET /api/evolve/system-prompt` ★ 新增
- **作用**：返回进化Agent静态蓝图（架构蓝图 Tab 数据源）
- **入参**：无
- **出参**：
  ```json
  {
    "blueprint": "<markdown 字符串，STATIC_BLUEPRINT 全文>",
    "version": "v0.2.23"
  }
  ```
- **不需要 session**：打开进化页即可调用

#### `POST /api/evolve/sessions/{id}/messages` ★ 新增
- **作用**：用户发消息，触发 Agent 对话一轮
- **入参**：`{ "content": "<markdown>" }`
- **出参**：`{ "message_id": "<uuid>", "seq": <int> }`
- **行为**：
  1. 写入 evolve_messages（role=user）
  2. 启动后台 task：`_run_converse_round`
  3. 立即返回（不阻塞，Agent 通过 SSE 推送回复）
- **错误**：session.status 不是 conversing → 409

#### `GET /api/evolve/sessions/{id}/messages` ★ 新增
- **作用**：获取历史消息（页面刷新恢复）
- **入参**：`?after_seq=<int>`（增量拉取，可选）
- **出参**：`{ "messages": [EvolveMessage, ...] }`

#### `GET /api/evolve/sessions/{id}/points` ★ 新增
- **作用**：获取进化点清单（浮窗数据源）
- **出参**：`{ "points": [EvolvePoint, ...] }`

#### `POST /api/evolve/sessions/{id}/finalize` ★ 新增
- **作用**：用户拍板，触发 finalizing
- **入参**：无（拍板 = 当前所有 accepted 进化点）
- **前置**：session.status=conversing 且至少 1 个 accepted 进化点
- **行为**：
  1. 从 accepted 进化点生成 design_doc.md（覆盖 docs.write_design_doc 逻辑）
  2. status=finalizing
  3. 启动后台 task：`_run_finalize_round`（system 触发消息：「根据已确认 design_doc 落地」）
  4. FlowGuard 解锁
- **错误**：无 accepted 进化点 → 400；status 非 conversing → 409

#### `GET /api/evolve/sessions/{id}/stream` ◐ 改造（保留路由，扩展事件）
- **事件类型扩展**：见 § 4.3 决策 T4
- **新增帧**：
  ```json
  // assistant_message
  {"type": "assistant_message", "message_id": "...", "content": "...", "related_points": [...]}
  // model_stream（增量 token）
  {"type": "model_stream", "message_id": "...", "delta": "..."}
  // tool_start / tool_end
  {"type": "tool_start", "tool": "...", "args_summary": "..."}
  {"type": "tool_end", "tool": "...", "result_summary": "..."}
  // proposal（进化点状态变更）
  {"type": "proposal", "point_id": "...", "action": "propose|update|reject", "data": {...}}
  // phase（阶段切换）
  {"type": "phase", "phase": "inspect|conversing|finalizing"}
  // finalizing（落地进度）
  {"type": "finalizing", "event": "edit|validate|change_log", "detail": "..."}
  // final（Agent 这一回合结束）
  {"type": "final", "status": "conversing|pending_review|failed"}
  ```

### 5.2 改造端点

#### `POST /api/evolve/start` ◐ 改造
- **新增行为**：启动后 status=running，立即触发 `_run_inspect_round`（探查阶段）
- 探查完自动转 conversing + Agent 发开场白
- **不再**直接跑完整 design_doc（这部分挪到 finalizing）

#### `POST /api/evolve/sessions/{id}/discard` ◐ 改造
- **新增行为**：discarded/failed 时清理对应的 checkpoint db 文件

---

## 6. 任务拆解 WBS（按依赖关系）

### Phase 1：后端基础层（数据 + 状态机）— 必须先做
**依赖**：无
**产出**：可独立测试的 DB + 状态机骨架

- **T1.1** DB 表迁移：evolve_messages + evolve_points（`db.py`）
- **T1.2** EvolveMessagesRepo + EvolvePointsRepo（新建 `evolve_repo.py`）
- **T1.3** EvolveContext 扩展：注入 points_repo / messages_repo / session_status（`ctx.py`）
- **T1.4** session.status 状态机扩展：running / conversing / finalizing（保留 pending_review / published / discarded / failed）

### Phase 2：后端 Agent 改造（核心）
**依赖**：Phase 1

- **T2.1** checkpoint_pool.py（per-session AsyncSqliteSaver）
- **T2.2** agent.py 改造：build_evolve_agent 加 checkpointer + thread_id
- **T2.3** agent.py 拆分：_run_inspect_round / _run_converse_round / _run_finalize_round
- **T2.4** prompt.py 拆分：STATIC_BLUEPRINT + _inject_dynamic
- **T2.5** tools.py 新增：propose_evolution_point / update_evolution_point / reject_evolution_point / finalize_evolution_plan
- **T2.6** flow_guard.py 改造：基于 session.status 的阶段门控
- **T2.7** event_sink.py 新建：EvolveEventSink + astream_events 转 SSE
- **T2.8** docs.py 改造：generate_design_doc_from_points

### Phase 3：后端 API 层
**依赖**：Phase 2

- **T3.1** GET /api/evolve/system-prompt
- **T3.2** POST /api/evolve/sessions/{id}/messages
- **T3.3** GET /api/evolve/sessions/{id}/messages
- **T3.4** GET /api/evolve/sessions/{id}/points
- **T3.5** POST /api/evolve/sessions/{id}/finalize
- **T3.6** 改造 POST /api/evolve/start（触发 inspect round）
- **T3.7** 改造 /discard（清理 checkpoint db）
- **T3.8** 改造 /stream（扩展 SSE 事件类型）

### Phase 4：前端基础层（可与 Phase 2-3 并行）
**依赖**：无（前端可 mock 数据先做）

- **T4.1** 新建 stores/evolveWorkbench.ts（zustand store）
- **T4.2** lib/api.ts 扩展：sendEvolveMessage / getEvolveMessages / getEvolvePoints / finalizeEvolve / getSystemPrompt
- **T4.3** lib/stream.ts 扩展：新 frame.type 分支处理

### Phase 5：前端组件层
**依赖**：Phase 4

- **T5.1** EvolveMarkdown.tsx（自定义渲染 [[finding:xx]] / [[propose:xx]]）
- **T5.2** EvolveMessage.tsx（消息卡片 + 内嵌引用 + 进化点卡片）
- **T5.3** EvolveProposalCard.tsx（参考 InterviewOptions）
- **T5.4** EvolveComposer.tsx（输入框，参考 chat-composer CSS）
- **T5.5** EvolveChatPanel.tsx（消息列表 + composer + progress track 容器）
- **T5.6** ProgressTrack.tsx（探查/落地进度事件流，折叠展示）
- **T5.7** EvolveDrawer.tsx（参考 TraceChainDrawer，浮窗 + 拍板按钮 + 扩展位）

### Phase 6：前端整合层
**依赖**：Phase 5 + Phase 3（API 就绪）

- **T6.1** BlueprintTab.tsx（GET system-prompt + react-markdown 渲染）
- **T6.2** WorkbenchTab.tsx（layout 容器：对话区 + 浮窗）
- **T6.3** SessionList.tsx（从 evolve.tsx 抽出，标"旧版"会话）
- **T6.4** evolve.tsx 重写：双 Tab 容器
- **T6.5** 双向高亮联动（浮窗 ↔ 消息区）
- **T6.6** finalizing → 自动跳 review-report
- **T6.7** styles/globals.css 改造：清理暗色残留 + 新增工作台样式

### Phase 7：联调 + 文档同步
**依赖**：全部

- **T7.1** 端到端联调：启动 → 对话 → 拍板 → 落地 → 发布
- **T7.2** 文档同步：`docs/系统心智模型.md` 图 3 evolution 端 + 图 4 前端
- **T7.3** AGENTS.md 收尾自检

---

## 7. § 实现风险清单（设计阶段识别）

> 本设计阶段新识别的实施风险（需求阶段未列出）。逐条标注消费方式。

| # | 风险 | 消费方式 | 状态 |
|---|---|---|---|
| R1 | LangGraph checkpoint 在 per-session SQLite 下，并发写同一 session 的 messages 表 vs checkpoint 可能竞争 | LangGraph 内部用 checkpointer 序列化访问；evolve_messages 表写入由 EvolveContext 单点管控；同一 session 同一时间只有一个 task 跑（按需触发模型保证） | 🟡 待实施验证 |
| R2 | astream_events 在工具调用频繁时可能产生大量事件，前端 SSE 流可能拥堵 | EvolveEventSink 聚合事件（tool_start/tool_end 只推摘要，不推完整 args/result），前端节流渲染 | 🟡 待实施验证 |
| R3 | 旧会话（v1-v5）进入新页面时浮窗/对话区无数据，可能渲染崩溃 | WorkbenchTab 检测 session 是否为旧版（无 evolve_messages 记录），显示"旧版本会话"提示 + 跳 review-report | 🟢 已消费（决策 S + T6.3） |
| R4 | propose 卡片的 options JSON 结构前端渲染时类型不匹配（缺字段/字段名错） | 前端 TypeScript 接口严格定义 + 后端 Pydantic 模型对齐 + 联调阶段强测 | 🟡 待实施验证 |
| R5 | 用户拍板后 finalizing 跑很久，前端可能误以为卡死 | ProgressTrack 实时显示落地进度事件（phase=finalizing + 每个 edit/validate 事件） | 🟢 已消费（决策 W + T5.6） |
| R6 | discard 清理 checkpoint db 文件时如果 Agent task 还在跑，可能删到正在使用的文件 | discard 前先 cancel task + 等 task 结束 + try/except 文件锁 | 🟡 待实施验证 |
| R7 | conversing 阶段 Agent 误用落地工具，FlowGuard 拦截后 Agent 怎么恢复 | FlowGuard 拦截后返回结构化错误信息告诉 Agent "当前是 conversing 阶段，不能调用 X 工具，请先和用户对齐"，Agent 会自我纠正 | 🟢 已消费（决策 T9） |
| R8 | 自定义 markdown 语法 `[[finding:xx]]` 与正常文本冲突（如代码块内的方括号） | remark 插件只在非代码块的非内联代码文本中匹配，代码块原样保留 | 🟡 待实施验证 |

---

## 8. MEMORY 更新自检（PR2）

本次设计**未触发 MEMORY 更新**。N/A · 原因：本次设计是具体功能改造方案，未引入新的项目级心智模型、约定或长期偏好，已有 MEMORY 记录的"全要素枚举"偏好已贯穿本次设计（决策树均穷举选项后让用户拍板）。

---

## § 待决策（持续追踪）

| # | 决策点 | 状态 | 备注 |
|---|---|---|---|
| ~~T1~~ | ~~对话循环路径~~ | ✅ 已定 | LangGraph 路径3（决策 T1） |
| ~~T2~~ | ~~对话循环运行时~~ | ✅ 已定 | 按需触发（决策 T2） |
| ~~T3~~ | ~~进化点存储~~ | ✅ 已定 | 纯 DB + 拍板生成文件（决策 T3） |
| ~~T4~~ | ~~SSE 事件源~~ | ✅ 已定 | EvolveEventSink + astream（决策 T4） |
| ~~T5~~ | ~~checkpointer 位置~~ | ✅ 已定 | 进化端独立 per-session（决策 T5） |
| ~~T6~~ | ~~消息表结构~~ | ✅ 已定 | 统一表 + role 区分（决策 T6） |
| ~~T7~~ | ~~进化点表结构~~ | ✅ 已定 | 完整结构表（决策 T7） |
| ~~T8~~ | ~~prompt.py 拆分~~ | ✅ 已定 | 函数级分离（决策 T8） |
| ~~T9~~ | ~~FlowGuard 门控~~ | ✅ 已定 | 读 status 拦截（决策 T9） |
| ~~T10~~ | ~~拍板状态流~~ | ✅ 已定 | 拍板触发→自动跳转（决策 T10） |
| T11 | 任务拆解（WBS） | 进行中 | 见 § 4 |
| T12 | 新增 API 端点清单 | 进行中 | 见 § 5 |
| T13 | 前端组件清单 | 进行中 | 见 § 6 |
