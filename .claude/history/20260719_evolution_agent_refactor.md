---
type: require
status: confirmed
created: 2026-07-19 00:00
confirmed: 2026-07-19 00:00
confirmed_by: 用户（27 个关注面 + 2 个收口细化全部拍板）
source: 进化Agent功能升级迭代重构：①系统提示词暴露架构全景+对创作Agent理解 ②页面美化 ③多轮对话让用户拍板进化点
related:
  - evolution/app/evolve/agent/prompt.py
  - evolution/app/evolve/agent/agent.py
  - evolution/app/evolve/api.py
  - evolution/desktop/src/pages/evolve.tsx
  - evolution/desktop/src/pages/review-report.tsx
---

# 进化Agent功能升级重构 — 需求基准

## 0. 核心诉求

把现在"启动→全自动跑完→pending_review→二选一发布/丢弃"的单向流程，
重构为"**和用户多轮对话式共创进化方案**"的交互形态：

1. **可见性**：进化Agent向用户展示自己脑子里的全景蓝图（系统提示词可视化）+ 对创作Agent的理解——让用户知道"它懂什么、能改什么、不能改什么"，建立信任。
2. **可控性**：进化过程从"Agent 跑完给成品"变为"逐个改进点和用户对齐"——给选项、给深度分析、用户拍板每个进化点，右侧浮窗实时汇总已确定的进化点，全部拍板后才发版。
3. **体验**：进化页面从草稿态升级为美观的工作台（对齐 trace-detail 的精致度）。

---

## 1. 现状速记（已探索确认）

### 1.1 进化Agent系统提示词现状
- 路径：`evolution/app/evolve/agent/prompt.py:18-34`（`evolve_system_prompt` 函数，硬编码 f-string）
- 已经是工程级蓝图：7 段全景（角色定位 / 能力边界 / 要素全景 / 装配+运行机理 / State约束 / 工作流 / 工具说明）
- 对创作Agent理解深入：知道 5 subagent（GP/interview/storybuilding/detail_outline/writing）、create_deep_agent 装配、middleware hook、NWM 记忆 6 要素、review-revise 循环
- 短板：硬编码字符串，不是模板文件；前端没有任何地方暴露这份提示词

### 1.2 进化流程现状
- 入口：`evolution/app/evolve/api.py:66`（`POST /api/evolve/start`）
- 状态机：running → pending_review → published / discarded（+ failed / cancelled）
- **已有"人审"环节**：Agent 跑完只到 pending_review，源码改动落在工作副本未 commit，必须用户在前端点发布/丢弃
- Agent 内部**全自动**：无 HITL interrupt，按 system prompt 流程跑到底
- 发版：publish_session（registry.publish_version + git commit_and_push + notify_executor + status=published）

### 1.3 前端进化页面现状
- 路径：`evolution/desktop/src/pages/evolve.tsx`（299 行，**草稿态**）
- 交互：左侧历史会话列表 + 右侧"选 trace 启动 + 实时日志流"，跳到 review-report 页才看到改动清单
- 技术：React 19 + Tailwind v4 + 自建 shadcn/Radix 组件库 + 暖色品牌 token + dark mode（但进化页用裸标签和原生 select）
- 美观度：⭐（明显掉档于 trace-detail ⭐⭐⭐⭐⭐ 和 review-report ⭐⭐⭐⭐）
- 残留技术债：CSS 暗色 fallback（`var(--surface, #1a1f2e)`）与暖色 token 冲突；旧 A/B 字段（baseline/candidate score/trace）前端仍在读

### 1.4 已有可复用资产
- ChatPanel（创作端 desktop）—— 消息列表 + 输入框 + 流式拼接范式，CSS 已搬到 evolution/desktop
- TraceChainTimeline / TraceChainDrawer（layout 内嵌右侧栏，不挡滚动）—— "流式步骤时间线 + 右侧详情"的最佳范式
- InterviewOptions（创作端 desktop）—— HITL 卡片化选项 + 单/多选 + 自定义输入组件
- ChangeCard（review-report）—— 改动卡片完整范式（序号+标题+状态徽章+多字段+折叠详情）
- shadcn Sheet / Tabs / Badge（含 running/completed/failed 语义色）/ Button / Dropdown

---

## 2. 全部已确认决策（A–AA，共 26 项）

### A. 对话形态 = 对话驱动式（最自由）
不预设阶段。用户和 Agent 自由对话，改进点在对话中浮现。右侧浮窗实时维护"已确定进化点"清单。最终用户拍板全部进化点后才进入"落地编码+发版"环节。

### B. 进化点状态对象 = 双轨制
- Agent 用工具调用作为**权威状态变更**：propose_evolution_point / update_evolution_point / reject_evolution_point / finalize_evolution_plan
- Agent 在自由文本里**讨论**改进点（探讨利弊、给备选、问用户）—— 这些不进浮窗
- 前端浮窗**只认工具调用**，状态准确、可持久化、刷新不丢

### C. 拍板 = 用户主动点击
右侧浮窗底部始终有"确认全部进化点，开始落地"按钮。用户聊到满意就点。Agent 不催促用户。

### D. 拍板后一次性落地，清单冻结
状态机：
```
running(读评估+探查) → conversing(对话共创) → finalizing(落地) → pending_review → published/discarded
                                                       ↓ 失败
                                                    failed
```
finalizing 中不能返回 conversing，不能改进化点。

### E. 进化点粒度由 Agent 动态决定
Agent 根据评估报告决定粒度，propose 时给出理由。用户可要求拆分/合并。

### F. 页面结构 = 双 Tab 布局
- **Tab 1「架构蓝图」**：只读展示系统提示词全文（结构化渲染）。不可编辑。
- **Tab 2「进化工作台」**（主页）：中部对话区 + 右侧浮窗。

### G. 单会话锁（保持现状）
同一时间只允许一个 conversing 会话。

### H. 对话完全持久化
对话原文 + 进化点状态全部入库。刷新/重开可恢复完整上下文。

### I. 落地失败 = 丢弃重开
finalizing 失败 → status=failed。git working tree 在 discard 时自动 git reset 回 baseline。不返回对话，不自动重试。

### J. Agent 主动开场
会话启动后 Agent 立即发出第一条消息（总结评估报告 + 提出本次进化要讨论的问题）。

### K. 评估/trace 隐式引用
评估报告和 trace 作为后台素材，Agent 通过 read_eval_report / read_trace 按需读取。提到某 finding/步骤时，在对话气泡内用折叠引用形式展示。

### L. 停止按钮 = 只停输出，会话保留
打断当前输出，状态保留，用户可继续输入。

### M. 浮窗 = 纯展示（镜子），前端架构预留扩展位
浮窗只展示进化点列表及状态。所有操作只在对话区进行。浮窗本身没有 accept/reject 按钮。
- **前端架构预留扩展位**：浮窗组件结构上预留 accept/reject/edit 等快捷操作的接入点（V1 隐藏不显示），未来如果发现"每次都要在对话框打字"太啰嗦，可一键开启快捷按钮，不需要重构组件。

### N. 浮窗 ↔ 对话双向高亮联动
- 点击浮窗进化点 → 对话滚动到讨论位置 + 高亮
- 对话里 hover/scroll 到讨论 → 浮窗该点高亮
- 联动通过"进化点 id"在消息里打标记实现

### O. 美观度对标 trace-detail
清理进化 CSS 暗色残留，与品牌 token 对齐。同等信息密度与精致度。

### P. 历史会话只读可查
published/discarded 会话进入"只读模式"——可查看对话、进化点、design_doc、change_log，但不能继续聊。

### Q. 架构蓝图 = 后端动态返回（静态骨架）
新增 `GET /api/evolve/system-prompt` 返回 prompt.py 中 `evolve_system_prompt` 的**静态骨架部分**——即不依赖 session 上下文的 7 段全景（角色定位 / 能力边界 / 要素全景 / 装配+运行机理 / State约束 / 工作流 / 工具说明 + 对创作Agent的理解）。

- **用户打开进化页即可看到**，不需要先启动会话
- 动态注入部分（session_id / trace_id / eval_summary / reflections_summary / memory_section）是会话运行时专属数据，不属于"蓝图"，蓝图 Tab 不展示
- 顺带给 prompt.py 一个干净的重构方向：分离"静态骨架"和"动态注入"（解决硬编码 f-string 短板）
- prompt.py 静态骨架修改后前端自动同步

### R. 架构蓝图 Tab = 进化页内部 Tab
进化页顶部 Radix Tabs：「架构蓝图」|「进化工作台」。

### S. 新老数据并存，旧会话标记为"旧版"
evolve_sessions 表保留不动，新增 evolve_messages 和 evolve_points 两张表。旧会话进入后显示"旧版本会话，不支持对话式查看"，只能跳 review-report。
- 旧 A/B 字段在新流程里完全不读不写（DB 列保留，代码层面废弃）

### T. propose 完整结构
一次 propose_evolution_point 输出：
- `id` / `target`（要改的要素）/ `problem`（为什么改，引用 finding id）
- `options[]`：2-3 个备选方案，每个含 `description / pros / cons / expected_impact`
- `recommendation`（推荐哪个 + 理由）/ `note`（补充说明）

用户回应可以是：选某个 option / 否决 / 要求补充 / 自定义方案。

### U. 保留 design_doc.md，从进化点表生成
拍板后从进化点表自动生成 design_doc.md（保持与现有 review-report / publish_session / registry 流程兼容）。design_doc 作为"进化点表快照"。

### V. review-report 页保留，与新工作台职责分离
- 工作台：对话共创 + 拍板 + 触发落地
- review-report：pending_review 最终审查 + 发布/丢弃

### W. 落地阶段 = 实时进度事件流
finalizing 阶段对话区变为"系统进度事件"流（类 trace-detail 节点时间线）：改了哪个文件 / 跑了什么 validate / 产生什么结果。用户可见但不可干预。

### X. 用户消息 = 纯文本 + markdown
不支持图片/附件上传。

### Y. Agent 输出 = 丰富渲染
- markdown（标题/列表/表格/代码块）
- 内嵌折叠引用（点击展开 finding / trace 步骤）
- 进化点 propose 卡片：结构化渲染
- 自定义 markdown renderer 识别特殊语法（如 `[[finding:F-001]]` / `[[propose:EP-2]]`）

### Z. FlowGuard 阶段门控
- conversing 阶段：只读探查工具 + 进化点工具可用
- conversing 阶段：**落地工具（edit_source / write_* / validate_changes / write_design_doc / write_change_log）被 FlowGuard 拦截**
- finalizing 阶段（用户拍板触发）：落地工具解锁
- 硬保证"未拍板不改码"，不靠提示词自律

### AA. finalizing 完成 = 自动跳转 review-report
finalizing 完成 → status=pending_review → 自动跳转 review-report 页。

---

## 3. 关注面覆盖全景（收尾自检）

| # | 关注面 | 状态 | 决策 |
|---|---|---|---|
| 1 | 系统提示词可见性 | ✅ | F |
| 2 | 多会话并行 | ✅ | G |
| 3 | 对话持久化 | ✅ | H |
| 4 | 落地失败处理 | ✅ | I |
| 5 | 美观度对标 | ✅ | O |
| 6 | 历史会话查看 | ✅ | P |
| 7 | 蓝图 Tab 位置 | ✅ | R |
| 8 | 老数据迁移 | ✅ | S |
| 9 | 蓝图数据来源 | ✅ | Q |
| 10 | 对话起始触发 | ✅ | J |
| 11 | 评估/trace 引用 | ✅ | K |
| 12 | 停止语义 | ✅ | L |
| 13 | 浮窗交互 | ✅ | M |
| 14 | 浮窗↔对话联动 | ✅ | N |
| 15 | 对话形态 | ✅ | A |
| 16 | 进化点状态对象 | ✅ | B |
| 17 | 拍板触发 | ✅ | C |
| 18 | 拍板后能否改进化点 | ✅ | D |
| 19 | 进化点粒度 | ✅ | E |
| 20 | propose 结构 | ✅ | T |
| 21 | design_doc 去留 | ✅ | U |
| 22 | review-report 去留 | ✅ | V |
| 23 | 落地过程可视化 | ✅ | W |
| 24 | 用户消息能力 | ✅ | X |
| 25 | Agent 输出复杂度 | ✅ | Y |
| 26 | 落地工具门控 | ✅ | Z |
| 27 | finalizing→review-report 过渡 | ✅ | AA |

全部 27 个关注面已覆盖，无遗漏。

### 决策 F：页面结构 = 双 Tab 布局
- **Tab 1「架构蓝图」**：只读展示系统提示词全文（结构化渲染：角色/能力边界/要素全景/机理图/工具清单/对创作Agent的理解）。不可编辑。用于建立用户对Agent判断的信任。
- **Tab 2「进化工作台」**（主页）：中部对话区（与Agent共创）+ 右侧浮窗（已确定进化点清单 + 拍板按钮）

### 决策 G：单会话锁（保持现状）
同一时间只允许一个 conversing 会话。working 区锁定机制保留。开新会话必须先把当前的推进到 pending_review / discarded。

### 决策 H：对话完全持久化
对话原文 + 进化点状态全部持久化。用户刷新/重开应用可恢复完整对话上下文和进化点清单。
**含义**：后端需要新增"对话消息存储"（evolve_messages 表或文件），SSE 重连后能续传。

### 决策 I：落地失败 = 丢弃重开
进入 finalizing 后失败 → status=failed，用户可"查看失败原因"或"丢弃会话"。git working tree 在 discard 时自动 git reset 回 baseline。不返回对话阶段，不自动重试。责任边界清晰。

### 决策 J：Agent 主动开场
会话启动后，Agent 立即发出第一条消息（总结评估报告 + 提出本次进化要讨论的问题），不等用户先说话。

### 决策 K：评估/trace 隐式引用
评估报告和 trace 作为"后台素材"，Agent 通过现有 read_eval_report / read_trace 工具按需读取。Agent 提到某 finding/步骤时，在对话气泡内用"折叠引用/链接"形式展示，不独立占屏。

### 决策 L：停止按钮 = 只停输出，会话保留
停止按钮仅在 Agent 输出过程中生效——打断当前输出，进化点清单和对话状态保留，用户可继续输入。不会取消整个会话。

### 决策 M：右侧浮窗 = 纯展示（镜子）
浮窗只展示进化点列表及其状态（proposed/accepted/rejected）。所有操作（选方案、否决、修改）只在对话区进行——用户在对话框里说"选A"或"不要第3个"，Agent 调工具更新状态，浮窗实时同步。浮窗本身没有 accept/reject 按钮。

### 决策 N：浮窗 ↔ 对话双向高亮联动
- 点击浮窗某个进化点 → 对话区自动滚动到该点被讨论的位置 + 高亮
- 对话里 hover/scroll 到某进化点的讨论 → 浮窗该点高亮
- 联动逻辑通过"进化点 id"在对话消息里打标记实现

### 决策 O：美观度对标 trace-detail
重写后进化页的视觉精致度对标 trace-detail 页（同套暖色 token、shadcn 组件、SVG/动效、类似信息密度）。清理进化相关 CSS 里的暗色 fallback 残留，与品牌 token 对齐。

### 决策 P：历史会话只读可查
published/discarded 的会话进入"只读模式"——可查看当时的对话、进化点、design_doc、change_log，但不能继续聊。进化页未启动新会话时默认展示历史列表。重开需"基于该会话创建新会话"。

### 决策 Q：架构蓝图 = 后端动态返回
新增 API（如 `GET /api/evolve/system-prompt`）返回 prompt.py 中 `evolve_system_prompt` 的完整内容，前端拉取后结构化渲染。prompt.py 修改后前端自动同步，不脱钩。

### 决策 R：架构蓝图 Tab = 进化页内部 Tab
进化页内部顶部 Radix Tabs：「架构蓝图」|「进化工作台」两个 Tab。架构蓝图是背景知识，随时可切过去查阅，不干扰工作台。与 harness 页 Tab 结构一致。

### 决策 S：新老数据并存，旧会话标记为"旧版"
evolve_sessions 表保留不动，新增 evolve_messages 和 evolve_points 两张表存储对话和进化点。旧会话（v1-v5 那些）在列表里仍可见，进入后显示"旧版本会话，不支持对话式查看"提示，只能跳到 review-report 看当时的报告。

**遗留技术债清理范围**（决策 S 衍生）：
- 旧 A/B 字段（baseline_score / candidate_score / candidate_trace / phase）在**新流程**里完全不读不写
- 前端 evolve.tsx 重写时不再读这些字段
- 数据库表保留这些列（避免迁移风险），但代码层面视作废弃

### 决策 T：propose 完整结构
一次 propose_evolution_point 输出字段：
- `id`：进化点唯一标识
- `target`：要改的要素（如 `meta_system.md` / `RetryMiddleware` / `memory_recall_middleware`）
- `problem`：为什么要改（引用评估 finding id）
- `options`：2-3 个备选方案，每个含 `description / pros / cons / expected_impact`
- `recommendation`：Agent 推荐哪个 option（含理由）
- `note`：Agent 的补充说明

用户回应可以是：选某个 option / 否决 / 要求补充 / 自定义方案。

### 决策 U：保留 design_doc.md，从进化点表生成
拍板后从进化点表自动生成 design_doc.md（保持与现有 review-report 兼容），落地后再生成 change_log.md。
- 优点：向后兼容、review-report 不需重写、publish_session / registry 流程不变
- design_doc 作为"进化点表的快照"，运行时以进化点表为准

### 决策 V：review-report 页保留，与新工作台职责分离
- **新工作台**（进化页 Tab 2）：负责"对话共创 + 拍板 + 触发落地"
- **review-report 页**（保留）：负责"pending_review 时的最终审查 + 发布/丢弃"
- 落地完成后从工作台跳到 review-report，保持发版前的独立审查仪式感

### 决策 W：落地阶段 = 实时进度事件流
finalizing 阶段对话区变为"系统进度事件"流（类似 trace-detail 节点时间线）：Agent 改了哪个文件 / 跑了什么 validate / 产生什么结果。用户看得到过程但不能干预。完成后跳 review-report。

### 决策 X：用户消息 = 纯文本 + markdown
不支持图片/附件上传。进化讨论以文字表达观点为主。

### 决策 Y：Agent 输出 = 丰富渲染
- 支持 markdown（标题/列表/表格/代码块）—— 现有 react-markdown + remark-gfm
- 内嵌折叠引用（点击展开评估 finding / trace 步骤详情）
- 进化点 propose 卡片：结构化渲染（target/problem/options[]/recommendation/note）
- 需要自定义 markdown renderer 识别特殊语法（如 `[[finding:F-001]]` 渲染为折叠引用、`[[propose:EP-2]]` 渲染为进化点卡片）

### 决策 Z：FlowGuard 阶段门控
- conversing 阶段：Agent 只能用只读探查工具（read_eval_report / read_trace / inspect_*）+ 进化点工具（propose/update/reject/finalize）
- conversing 阶段：**落地工具（edit_source / write_* / validate_changes / write_design_doc / write_change_log）被 FlowGuard 拦截**
- finalizing 阶段（用户拍板触发）：Agent 获取"落地授权"，落地工具解锁
- 这是"未拍板不改码"的硬保证，不靠提示词自律

### 决策 AA：finalizing 完成 = 自动跳转 review-report
finalizing 阶段 Agent 落地完成后自动跳转 review-report 页（status=pending_review），用户在 review-report 完成最终发布/丢弃。保持职责分离的仪式感。

---

## 3. 已浮现但未确认的关注面（持续追踪）

| # | 关注面 | 状态 | 备注 |
|---|---|---|---|
| 1 | ~~系统提示词"可视化"具体含义~~ | ✅ 已定 | 只读展示（决策 F） |
| 2 | ~~多会话并行~~ | ✅ 已定 | 单会话锁（决策 G） |
| 3 | ~~对话状态持久化~~ | ✅ 已定 | 完全持久化（决策 H） |
| 4 | ~~落地失败的具体处理~~ | ✅ 已定 | 丢弃重开（决策 I） |
| 5 | 美观度的具体对标 | 待澄清 | 用户说"不要草稿"，对标 trace-detail 还是 review-report？还是更高？ |
| 6 | 对话历史归档 | 待澄清 | published/discarded 后对话历史要不要保留可查？ |
| 7 | 与现有 review-report 页关系 | 待澄清 | 新页面替换 evolve.tsx，还是 review-report 也一起改？ |
| 8 | 老数据迁移 | 待澄清 | 现有 evolve_sessions（旧 A/B 字段残留）如何处理？ |
| 9 | 架构蓝图 Tab 数据来源 | 待澄清 | 后端暴露 prompt.py？硬编码前端？ |
| 10 | ~~对话起始触发~~ | ✅ 已定 | Agent 主动开场（决策 J） |
| 11 | ~~评估报告/trace 在对话中如何引用~~ | ✅ 已定 | 隐式引用（决策 K） |
| 12 | ~~对话中断（停止/网络断）~~ | ✅ 已定 | 只停输出，会话保留（决策 L） |
| 13 | ~~浮窗内的进化点可交互吗~~ | ✅ 已定 | 纯展示（决策 M） |
| 14 | 一个进化点的"完整决策流" | 待澄清 | propose→用户问→Agent答→用户选→update, 具体一次交互多久? |
| 15 | ~~浮窗/对话的视觉风格细节~~ | ✅ 已定 | 对齐 trace-detail（决策 O） |
| 16 | ~~架构蓝图 Tab 与现有页面关系~~ | ✅ 已定 | 进化页内部 Tab（决策 R） |
| 17 | ~~老数据迁移~~ | ✅ 已定 | 新老并存（决策 S） |
| 18 | ~~进化点 propose 的内容结构~~ | ✅ 已定 | 完整结构（决策 T） |
| 19 | ~~Agent 落地阶段是否还产出 design_doc~~ | ✅ 已定 | 保留，从进化点表生成（决策 U） |
| 20 | ~~review-report 页是否同步改造~~ | ✅ 已定 | 保留，职责分离（决策 V） |
| 21 | ~~对话消息渲染格式~~ | ✅ 已定 | 丰富渲染（决策 Y） |
| 22 | ~~用户消息能力~~ | ✅ 已定 | 纯文本+markdown（决策 X） |
| 23 | ~~落地过程可视化~~ | ✅ 已定 | 实时进度事件（决策 W） |

