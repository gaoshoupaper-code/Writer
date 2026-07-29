# Writer Trace 统一裁剪报告

> 调研范围：最近 12 个月及当前官方资料；证据目录见 [evidence_catalog.md](evidence_catalog.md)。
>
> 结论状态：13/13 项结构化研究均通过 22/22 必填字段校验。本报告只提出与 Writer 当前 `executor + evolution + DeepAgent/Harness` 架构直接相关的机制，不以接入某个观测产品为目标。

## 1. 决策结论

Writer 不应建设两套业务 Trace 系统，也不应继续把所有 Trace 混在进化端一个“监测大盘”中。

应建设的是：**一套 Writer 内部 canonical Trace 契约、关联协议和查询底座；三类职责明确的消费工作台；按工作负载分开的指标、页面和保留策略。**

| 决策 | 结论 |
|---|---|
| Trace 边界 | 一次创作请求一条根 Trace；证据编译、评估、进化各自创建新根 Trace。同步 Tool/Subagent 是当前 Trace 的子 Span；异步流程通过 SpanLink、来源 Trace 和领域对象关系连接。 |
| 真相源 | Writer 自有事件、PayloadObject、ArtifactRevision、卷宗、评估、实验和发布对象是唯一真相源。OTLP、Gateway/APM、会话 transcript 和产品 dashboard 只可做可丢弃的诊断副本。 |
| 采集原则 | 在事实发生处采集：DeepAgent/Harness 采运行语义，业务服务采制品和后续结果，摄入端审计完整性。禁止通过 `agent_name`、`read_file(SKILL.md)`、workspace 当前文件或外部 Trace 事后猜测。 |
| 数据原则 | 保存分析所需的完整 Prompt、稿件和 Tool 语义，但先经过 PayloadGate；秘密、认证信息、环境变量、CoT、embedding、二进制和媒体正文永不进入 Trace。 |
| 消费闸门 | 创作可在观测故障后完成，但须标记 `trace_incomplete`；证据、评估、进化不得消费 `integrity_status != verified` 的来源。 |
| 外部标准 | W3C Trace Context 用于可信跨服务传播；OTel/OTLP 仅做版本化、单向、脱敏导出。OpenInference、ATIF、OpenLineage 只保留映射边界，不做首期 SDK、导入器或双向同步。 |

这是一套“专而精”的设计：它解释写作运行、证据卷宗、评估和进化闭环所必需的因果，不试图成为通用 APM、Agent 平台或数据治理平台。

## 2. 当前系统的真实位置

现有系统不是从零开始。共享契约 [contracts/trace/__init__.py](../contracts/trace/__init__.py) 已定义 `TraceLogEvent`、稳定 `event_id`、单调 `sequence`、`run_id/parent_run_id`、锚点和 LLM/Tool 生命周期。执行端 [recorder.py](../executor/app/platform/trace/recorder.py) 以异步队列和 JSONL 落盘；进化端 [recorder.py](../evolution/app/trace/recorder.py) 以 DB 主存储加 JSONL WAL，摄入端已有增量读取和节点投影。这些基础应保留并演进。

| 现状 | 已有价值 | 目标差距 |
|---|---|---|
| `run/llm/tool` 事件与节点投影 | 能看单次技术执行树、Token、错误、上下文锚点和基础父子关系。 | 没有稳定的 `schema_version`、SpanLink、工作负载/用途语义、逻辑 attempt、完整性清单或跨服务因果对象。 |
| `skill_name` 和 `skill` 节点 | 有初步 Skill 可见性。 | 当前主要从文件访问/事件后推断，无法区分可用、读取、实际激活、完成、失败，也没有版本、来源和内容 hash。 |
| `source=middleware` | 能标识部分事件来自中间件。 | 没有最终有序装配清单，不能解释哪一个 middleware 实际修改、阻断、路由、缓存、压缩或触发 HITL。 |
| JSONL、DB/WAL、sequence | 已具备稳定事件标识、增量摄入和恢复基础。 | 当前摄入以最大 `sequence` 推进水位，`1,2,4` 可能永久跨过缺失的 `3`；还缺 terminal manifest、连续前缀 receipt、事件/序号唯一冲突审计、摄入覆盖状态和下游消费闸门。 |
| `input/output/tool_args/tool_output` 原始字段 | 能满足当前调试。 | 正文直接附着事件，缺少类型化 PayloadObject、源端拒绝、访问审计、生命周期和删除关联。 |
| Artifact 页面与 workspace | 能显示当前产物，现有快照中已有局部产物线索。 | 历史 Trace 可能回读 mutable workspace；现有 ArtifactSnapshot 仍只是 run metadata，缺少统一写后回读/hash/revision 证明，不能作为当时用过或产出的版本。 |
| `monitor.tsx` 的创作/进化 Tab | 已开始区分来源。 | 全局概览、Agent 调用统计和失败模式仍混合；其中“Skill 统计”实际按 `agent_name` 聚合，不是 Skill 指标。 |

因此，现有实现足以保留为**事件运输和单次运行投影底座**，但还不能可靠回答最终目标问题：发生了什么、哪个机制介入、输入输出制品是什么、使用了哪些版本、后续质量是否改善。

## 3. 目标领域模型：一套契约，四类对象

不要把所有信息塞回 `TraceLogEvent`。将运行、语义载荷、长期制品和结果事实分开，使用稳定 ID 关联。

| 对象 | 职责与最小内容 | 与现有模型的关系 |
|---|---|---|
| `TraceRun` / `Span` / `TraceEvent` | 有界执行。`TraceRun` 含 `trace_id`、`service`、`workload`、`purpose`、根状态、harness/model 配置版本；`Span` 只表示 Agent、Subagent、LLM、Tool 等同步执行；`TraceEvent` 表示生命周期和机制事实。 | 扩展现有 `TraceRunSummary`、`TraceNode`、`TraceLogEvent`，保持已有 `event_id`、`sequence`、anchor 兼容。 |
| `PayloadObject` | 经过策略治理的完整语义内容。含 `payload_id`、`payload_kind`、`classification`、`content_ref`、hash、大小、策略版本、redaction manifest、创建/过期时间和生产者引用。 | 将当前事件内的正文变为引用；热索引只保留结构、摘要、hash 和引用。 |
| `Artifact` / `ArtifactRevision` | 需要跨运行复查、评估、发布或审计的持久交付物。revision 含 `revision_id`、父 revision、hash、内容引用、生产 Trace/Span/Event、harness 版本和创建时间。 | 新增；路径只是属性，不能再充当历史真相。 |
| `Outcome` / `Score` / `Experiment` / `Release` | 运行后发生的行为、测量、候选比较和发布事实。它们共享关联协议，但不是另一套 Trace。 | 新增轻量领域记录，复用既有卷宗、benchmark 和 registry，不建设通用实验平台。 |

### 3.1 必须新增的一等事件

以下不是“可选 metadata”，而是 Writer 能解释自身机制的最低事实。

| 事件 | 何时产生 | 必须记录 | 不记录什么 |
|---|---|---|---|
| `skill.activation` | 受控 loader 已解析并决定将 Skill 内容投入当前 Agent 上下文时。 | `activation_id`、`skill_id`、来源、manifest/version、`definition_sha256`、自动/手动触发、触发位置、所属 Span、开始/完成/失败/取消状态。 | 普通 `read_file`、可用目录扫描和未激活 Skill 不能冒充激活。 |
| `middleware.assembly` | DeepAgent/Harness 已完成最终实例合并、替换和排序后。 | `stack_id`、Agent scope、外到内顺序、middleware ID、来源、版本、implementation/config hash、支持 hook 集和启用状态。 | 传入参数、目录扫描或未实际装配的 middleware。 |
| `middleware.intervention` | 真实生效的代码分支发生时。 | `intervention_id`、stack position、hook、动作词、原因/策略版本、目标 Span 或 Tool call、受影响字段、before/after payload ref、结果、耗时、attempt。 | no-op、cache miss、校验通过、未达到摘要阈值等噪声。 |
| `hitl.request` / `hitl.decision` | 请求人工决策、收到批准/拒绝/超时及实际恢复时。 | policy/version、请求对象引用、actor、决定、时间、恢复或阻断结果。 | 不能把“有权限层”或“模型自评安全”当成真实人工决定。 |
| `trace.manifest` | 根 Trace 进入终态且本地 canonical 事件已持久化后。 | terminal status、`final_sequence`、事件数、terminal event ID、序列摘要、必需对象/版本引用、delivery 状态。 | 不以 HTTP 202、外部 dashboard 或队列成功代替完成清单。 |

第一期 `middleware.intervention.action_type` 固定为：`modify`、`block`、`route`、`retry`、`recover`、`cache_hit`、`context_compact`、`hitl`、`short_circuit`、`coordination_wait`。封闭词表是为了可查询、可比较，而不是限制以后有明确需求时再版本化扩展。

### 3.2 因果关系不能只靠一棵树

同步层级使用 parent-child；异步和长期业务关系使用显式边。

| 场景 | 正确关系 | 错误做法 |
|---|---|---|
| 创作中的 LLM、Tool、Subagent | 当前根 Trace 内的子 Span。 | 为每个 Hook 产生 Span，或只用 Agent 名称猜父子关系。 |
| executor 同步通知 evolution | 可信 W3C `traceparent` 的远程父子 Span；Writer `trace_id` 仍是业务主键。 | 用 OTel TraceId 替换 `trace_id` 或把业务对象放入 `tracestate`。 |
| evolution 收到 202 后异步 ingest | 新 Trace/新处理 Span，`SpanLink` 指向触发上下文，并带 `source_trace_id`。 | 把数小时后的任务挂在原创作 Trace 下。 |
| 证据编译、评估、进化、发布 | 新根 Trace + 领域边：`triggered_by`、`retry_of`、`resumed_from`、`derived_from`、`consumes`、`produces`。 | 将整条创作到发布链塞成一条永不结束的 Trace。 |
| 批处理多个来源 | 每个输入一个 Link 和来源对象 ID。 | 伪造一个多父 Span 树。 |

`trace_id`、`artifact_revision_id`、`dossier_id/version`、`evaluation_id`、`experiment_id`、`release_id` 是 Writer 的持久血缘键。OTel trace/span ID、gateway request ID、provider response ID 只进入 `external_refs`。

## 4. 采集、存储和摄入：沿用现有双端边界

### 4.1 端到端职责

1. **executor 创作端**：在 DeepAgent/Harness 生命周期内构造 typed 事实，先经过 PayloadGate，再写本地 canonical JSONL/outbox。创作路径不得同步等待 evolution 或外部观测平台。
2. **executor 本地可靠层**：保留稳定 `event_id`、单调 `sequence` 与现有 JSONL；终态前同步 flush 并写 `trace.manifest`。本地写入失败不阻断创作，但写入 `capture_degraded` 并使根 Trace 成为 `trace_incomplete`。
3. **evolution 摄入端**：继续以主动 pull/scan 为主、notify 为低延迟提示。对 `(trace_id, event_id)` 和 `(trace_id, sequence)` 幂等 upsert；同键不同摘要记录 `integrity_conflict`，不得静默覆盖。每条 Trace 的 receipt 维护 `contiguous_seq`、`max_seen_seq`、`missing_ranges`、terminal manifest 与 receipt revision，不能把最大序号误当已完整摄入。
4. **evolution 业务端**：在已验证完整的来源上运行证据编译、评估和进化；每个流程自己的 recorder 建新根 Trace，并写出卷宗、评分、实验和发布关系。
5. **可选兼容出口**：canonical 事件持久化后，才单向投影去 OTLP 或选定外部后端。导出失败永远不回滚创作、卷宗或内部 Trace。

### 4.2 完整性与 coverage 必须分开

| 维度 | 含义 | 影响 |
|---|---|---|
| `run_status` | 业务执行结果：running、awaiting_input、completed、failed、cancelled、interrupted。 | 解释运行本身。 |
| `integrity_status` | `verified`、`incomplete`、`conflict`、`legacy`。检查连续 sequence、终态 manifest、必需父子关系、来源 Link、关键版本/制品引用和 PayloadObject 可解析性；`trace_incomplete` 是所有非 `verified` 状态的面向运行提示。 | `incomplete/conflict/legacy` 不能进入证据、评估、进化数据集。 |
| `coverage` | 可选事实是否可得：Token、成本、provider attempt、外部导出、采样、payload 截断等以 `known/partial/unknown/not_applicable` 表示。 | 降低分析可信度，不把未知自动变成失败或零。 |
| `delivery_status` | 本地持久化、通知、pull、摄入、外部导出的状态。 | 用于诊断和重试；外部 delivery 不决定 canonical 完整性。 |

因此，模型不返回 token 会降低 `coverage`，但不会使 Trace 不完整；缺 terminal event、关键 ArtifactRevision 或 required Link 则使 `integrity_status=incomplete`，同键异载荷则为 `conflict`。创作仍可交付，后续高风险消费被明确阻断。

### 4.3 PayloadGate 是先决条件

PayloadGate 必须位于任何 recorder、摄入、UI 和 exporter 之前，按固定 `payload_kind` 执行字段 allowlist、禁止类型剥离、秘密检测、规范化和引用化。

| 类别 | 处理 |
|---|---|
| `semantic_full` | 保存已清洗的 effective model input、visible model output、effective Tool args/result、middleware before/after、HITL 请求/决定、稿件内容、人工编辑 patch、评估证据。 |
| `reference_only` | 媒体、外部文件、受管制制品仅保存引用、hash、类型和元数据。 |
| `structural` | 事件、版本、计数、状态、reason code、关系和摘要。 |
| `forbidden` | secret、Authorization、Cookie、环境变量、CoT、embedding、二进制和媒体正文永不写入。未知复杂对象默认拒绝。 |

完整内容默认保存 90 天；明确进入评估数据集或进化实验的快照可按选择封存更久。完整内容查看、搜索和导出仅向授权开发者/质量人员开放，且形成不可变访问审计。第三方 exporter 固定 no-body allowlist，不允许其配置反向放宽 Writer 策略。

## 5. 制品、卷宗、评估和结果血缘

### 5.1 不可变制品是长期解释的支点

`Artifact` 表示逻辑槽位，`ArtifactRevision` 表示一次写盘成功、回读成功并计算 hash 后的准确内容。每个 revision 至少带生产 Trace/Span/Event、父 revision、harness 版本和内容引用。只有需要跨运行复查、评估、发布或审计的持久交付物创建 revision，不把每个 Prompt、流式片段和普通读取都版本化。

固定的业务 DAG 边仅包括：

- Trace `produces` / `consumes` ArtifactRevision
- ArtifactRevision `compiled_into` EvidenceDossierVersion
- ArtifactRevision 或 EvidenceDossier `evaluated_by` EvaluationDossier / ScoreRecord
- ArtifactRevision / EvaluationDossier `compared_in` ExperimentRun
- Candidate `selected_for` ReleaseVersion
- ReleaseVersion `activated` / `rolled_back`

卷宗继续保留当前编译工作流，但 manifest/facts 必须列出 consumed/produced revision、缺失 revision 和 provenance。重编译创建新 dossier version 并 `supersedes` 旧版，不能原地改写。

### 5.2 运行后事实不回写已结束 Trace

复制、重新生成、最终采用、人工编辑差异、人工评分、自动评估、候选 uplift、发布和回滚发生在运行结束之后。它们以 `OutcomeRecord`、`ScoreRecord`、`ExperimentRun`、`ReleaseDecision/ReleaseOutcome` 独立持久化，并指向目标 Trace 或 ArtifactRevision。发布状态至少区分 `committed -> registry_promoted -> executor_refresh_ack -> activated`，并显式记录 `activation_failed` 与 `rollback_activated`；不能把“提交成功”误写为已经生效。

这既避免一条 Trace 无限增长，也允许分析时区分弱信号与强证据：复制、停留时长、重生成和编辑量不能折算成单一质量分；人工评分、版本化 rubric、明确 evidence 和最终采用才是较强质量证据。

## 6. 进化端：从混合监测页拆成三个工作台

当前 [monitor.tsx](../evolution/desktop/src/pages/monitor.tsx) 的创作/进化 Tab 可以作为迁移入口，但不能继续承担全部职责。目标 UI 不是三个隔离产品，而是在同一查询底座上的三个任务入口。

| 工作台 | 用户的问题 | 默认对象与查询 | 必须呈现 | 明确不呈现 |
|---|---|---|---|---|
| 运行观测 | 这一次运行发生了什么，哪个机制介入？ | Trace 为中心；按 service、workload、purpose、状态、integrity、coverage、harness/model/Skill 版本筛选。 | 调用树、时间线、版本快照、完整性、coverage、Skill 生命周期、Middleware 有效干预、HITL、错误、受控载荷与 external refs。 | 业务 DAG、长期排行榜、未授权正文。 |
| 血缘 | 这个稿件/卷宗/评估/发布从哪里来、影响哪里？ | ArtifactRevision 或 dossier/evaluation/release 为中心；只允许固定节点和边类型。 | 双向 DAG、版本、来源/去向、缺失或不完整节点、可跳转的运行详情。 | 把全部业务关系伪装成调用树，或开放任意节点/边。 |
| 分析 | 哪个 workload、版本或机制值得进一步比较？ | workload Profile 与时间/版本切片为中心。 | 公共健康指标、专属 Profile、Outcome/Score、样本量、coverage、完整性分母，并可回钻样本。 | 跨工作负载总成功率、单一质量分、无样本支持的自动因果结论。 |

所有图表必须显示来源、公式版本、时间窗、样本量和缺失语义。将当前“Agent 调用统计”更名或替换为真正的 `SkillActivation` 聚合；在迁移完成前明确标注为 Agent 调用，不能误导质量人员。

## 7. 指标设计：共享核心，工作负载分 Profile

| Profile | 主要指标 | 数据质量前提 |
|---|---|---|
| 公共核心 | 完成/失败/取消、吞吐、p50/p95/p99、错误与 retry、Token、成本、Span 数/深度、integrity、coverage、版本。 | 允许部分可选字段未知，但必须标明分母。 |
| 创作 | 首响应、HITL 等待、Subagent/Tool/Skill 选择、有效 Middleware 干预、ArtifactRevision、复制/再生成/采用、人工编辑差异、质量信号。 | 只分析完整 Trace；Outcome 明确强弱。 |
| 证据编译 | 阶段耗时、完整度、契约覆盖、证据数量、新鲜度/多样性、引用覆盖、unsupported claim、LLM 失败。 | 所有输入 revision 和 dossier provenance 可解析。 |
| 评估 | rubric 分数、finding 严重度、人工一致性、误报/漏报、evaluator/model 漂移、成本。 | rubric/evaluator/evidence 版本冻结。 |
| 进化 | baseline/candidate、样本切片、uplift、胜率、置信度、promotion/rollback、发布后结果。 | 不使用 incomplete、legacy 或 coverage 不足的数据作门禁。 |
| 观测管道 | 本地队列、摄入延迟、重复去重、sequence 缺口、冲突、脱敏拒绝、孤儿载荷、导出失败、删除状态。 | 与业务成功率分开显示。 |

## 8. 工业适配与刻意排除

| 分类 | Writer 采用 | 原因 |
|---|---|---|
| 核心机制 | 有界 Trace、同步 Span、异步 Link、版本化内部契约、PayloadGate、ArtifactRevision、完整性门、Outcome/Score/Release、三工作台。 | 直接回答 Writer 的创作到发布问题。 |
| 兼容机制 | W3C `traceparent`、薄 OTLP adapter、低敏 external refs、OpenAI-compatible `base_url`、未来按需 OpenInference/ATIF/OpenLineage 映射。 | 保留互操作与诊断出口，不污染领域语义。 |
| 明确排除 | 第三方 SaaS 真相源、通用网关/多厂商 SDK、Agent Teams/Cloud VM/worktree/PR、通用 Hook 脚本/权限/沙箱平台、技能市场、任意 dashboard、双向导入、Kafka/通用事件总线、exactly-once 外部投递、完整 CoT。 | 与当前 DeepAgent/Harness 和写作进化闭环无直接价值，且扩大依赖、数据风险和维护面。 |

成熟产品的有效启发是“把事实采在发生点”，不是“复制产品表面功能”。Claude Code 的 OTel 仍是 beta；Cursor/Hermes/OpenClaw 的能力依赖版本、配置和采样；OpenClaw 的外部 CLI 仅能看见 opaque turn；Codex 本次没有可访问的一手 Trace 文档。它们都不能替代 Writer 自己的领域契约。

## 9. 分期落地顺序

### 阶段 A：冻结契约与安全底线

1. 为现有事件信封增加 `schema_version`，定义向后兼容的 reader 和固定事件/动作词表。
2. 为 `(trace_id,event_id)`、`(trace_id,sequence)` 建立唯一性和冲突审计；以每 Trace 串行事务写事件、receipt、水位和投影，receipt 只推进连续前缀。
3. 实现 PayloadGate 与 PayloadObject；先禁止不可接受数据进入新 Trace，再迁移正文读取。
4. 定义 `TraceRun/Span/Event`、`integrity_status`、`coverage`、terminal manifest 和 legacy 映射；补齐契约 fixture 与序列/冲突测试。

验收：新 Trace 能被独立 reader 校验；receipt 只推进连续序列、能报告 `1,2,4` 的缺口、同键异载荷进入 conflict；秘密/CoT 进入 recorder 的测试失败；老 Trace 可读但明确标为 `legacy`，不伪造未知字段。

### 阶段 B：创作运行事实与可靠摄入

1. 在 DeepAgent/Harness 最终装配处写 MiddlewareAssembly，在唯一 Skill loader 写 SkillActivation，在真实干预分支写 Intervention。
2. 保留 executor JSONL 与 evolution DB/WAL，加入 manifest、稳定幂等键、final sequence 对比和 integrity auditor。
3. 创作端观测失败 fail-open 并标记 `trace_incomplete`；evolution 消费端对不完整来源 fail-closed。

验收：可从一次创作详情定位真实激活 Skill、最终 middleware 顺序、一次有效干预及其 before/after 引用；重复、乱序、缺 terminal 和冲突事件均有确定结果。

### 阶段 C：制品和跨流程血缘

1. 将稿件、卷宗、评估结果和候选配置接入 ArtifactRevision；停止历史详情回读 mutable workspace。
2. 证据编译、评估、进化新建根 Trace，写入明确 source/consumes/produces/retry 关系。
3. 增加 Outcome、Score、Experiment、Release 最小记录，先覆盖人工采用、编辑、评分、候选比较和发布/回滚。

验收：从任一 revision 可双向追溯到生产运行、卷宗、评估和发布；不完整来源不能进入下游流程。

### 阶段 D：进化端工作台与基础分析

1. 将现有监测页迁为运行观测入口，先按 `service/workload/purpose/integrity` 分开列表和指标。
2. 建立封闭业务 DAG 和版本详情，随后交付每个 workload 的基础 Profile 与数据质量提示。
3. 添加访问审计、完整内容按需读取和最小人工 Score。

验收：开发者可完成单次诊断，质量人员可审查完整内容访问和质量证据，两个角色不会在一个混合排行榜中得到错误结论。

### 阶段 E：兼容出口和数据成熟后能力

1. 在 canonical 事件稳定后接入 W3C/OTLP 单向投影和 external refs。
2. 仅在样本、rubric、coverage 和 Outcome 足够可信时，开放候选 uplift、切片回归和发布后比较。

验收：关闭任何外部平台后，Writer 的运行、血缘、评估和进化仍完整可用；外部导出只影响诊断副本。

## 10. 第一阶段验收清单

- 任一创作运行可明确识别 service、workload、purpose、根状态、完整性和完整同步父子关系。
- 真实 Tool/Subagent、SkillActivation、MiddlewareAssembly/Intervention、HITL 和 ArtifactRevision 均可在详情中定位；未激活 Skill、no-op hook 和模型 CoT 不出现为业务事实。
- 异步证据、评估、进化能双向导航到来源 Trace 和制品，但各自耗时/状态独立计算。
- `trace_incomplete` 的创作仍可完成；证据、评估、进化会拒绝该来源并给出明确原因。
- 历史 Trace 标为 `legacy`，不可恢复字段显示 unknown；不会由路径、名称或当前 workspace 内容补造。
- 完整 Prompt、稿件、Tool 参数/结果只有授权用户按需读取，所有查看、搜索和导出有审计；秘密和 CoT 永不进入 Trace。
- 运行观测、血缘、分析三个工作台的对象与指标分母清晰分离；每个分析图可回钻到实际样本。
- 外部 OTLP/Gateway/APM 不可用时不影响 Writer 主流程、内部完整性和业务 DAG。

## 11. 证据边界

本报告不承诺模型确定性回放，也不将低质量 Outcome 解释为因果。外部平台的采样、保留、重试、路由、cache、guardrail 和价格口径均可能形成未知区；必须以 `coverage` 和 `external_refs` 表示，而不是通过漂亮的 Trace 树掩盖。

完整逐项来源、标准成熟度、产品版本和不确定性边界保存在 [evidence_catalog.md](evidence_catalog.md) 及 `results/*.json`。本报告的约束优先级为：Writer 业务语义与安全策略 > 已冻结内部契约 > 开放标准兼容 > 单个产品能力。
