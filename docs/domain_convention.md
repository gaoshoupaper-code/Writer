# Domain 组织规约

执行端（executor）领域层的组织规范。新增 domain 时照此建立结构，保证 writing/image 及未来 domain 的组织一致性。

## 核心原则

- **domain = 领域知识的集合**。放领域专属逻辑（业务规则、领域服务、领域 LLM、领域数据模型）。不放领域无关的平台基础设施（那些在 `platform/`）。
- **核心文件固定，可选子目录按复杂度伸缩**。简单 domain（image）扁平几文件即可；复杂 domain（writing）允许多层子目录。
- **两种 domain 模式并存**：进化型（装配外包到 harness 包）和独立型（executor 内自装配）。

## 核心文件（必有）

```
domains/<name>/
  agent.py      # 主服务：装配 agent + 调 run_agent_stream + 响应构建
  models.py     # 领域 model 构建（build_<name>_model）
  router.py     # REST 端点 + init_<name>_routes 注入（main.py lifespan 调）
```

- **agent.py** 是 domain 的服务入口。命名统一为 `agent.py`（不用 `<name>_service.py` 或 `meta_service.py`）。
  - 进化型 domain：agent.py 是薄壳（构建 RuntimeContext + 调 `pkg.assemble(ctx)`）。
  - 独立型 domain：agent.py 内含完整装配（`create_deep_agent`）。
- **router.py** 用模块级全局 + `init_<name>_routes(service, ...)` 注入模式（main.py lifespan 调用）。

## 可选文件

```
  store.py      # 持久化（如有独立 artifact store，如 image 的 ImageArtifactStore）
  events.py     # 领域 EventSink（如 SSE 事件分发有领域逻辑，如 writing 的 WritingEventSink）
```

## 可选子目录（按复杂度伸缩）

```
  prompts/      # prompt 文件（独立型 domain 用；进化型的 prompt 在 harness 包内）
  providers/    # 外部服务适配（如 image 的 bytedance/ 图像供应商）
  tools/        # 领域专属工具
  services/     # 子服务（如 writing 的 CharacterService / storyline_graph）
  <子系统>/      # 领域子系统（如 writing 的 styling/ 风格优化子系统）
```

## 两种 Domain 模式

### 进化型 domain（如 writing）

agent 装配在 harness 包（`evolution/harnesses/current/`），可被进化端迭代优化。

- **executor 侧 agent.py**：薄壳。构建 RuntimeContext（含 model/backend/checkpointer/styles 等）+ 调 `pkg.assemble(ctx)` + SSE 编排。
- **装配逻辑**：在 harness 包 `__init__.py:assemble()`，含 prompt + subagent + middleware + skills。
- **特征**：domain 内**没有** prompts/ 子目录（prompt 在包里）；有 events.py（领域 SSE 逻辑）；可能有多个子服务/子系统。

```
domains/writing/        # 进化型
  agent.py              # 薄壳：RuntimeContext + 调包 + SSE
  models.py             # build_writer_model
  events.py             # WritingEventSink
  deepseek_thinking.py  # 领域 LLM 扩展
  styling/              # 风格子系统
  expert_agent/services/# CharacterService + storyline_graph
  # 无 prompts/（在 harness 包内）
```

### 独立型 domain（如 image）

agent 装配在 executor 内，不参与进化。

- **executor 侧 agent.py**：完整装配。内含 `create_deep_agent`（tools/middleware/skills 全在 domain 内组装）。
- **特征**：domain 内有 prompts/（prompt 文件在本域）；可能有 providers/（外部服务适配）。

```
domains/image/          # 独立型
  agent.py              # 完整装配：create_deep_agent + SSE
  models.py             # build_image_model
  router.py             # 文生图端点 + Skill 管理
  store.py              # ImageArtifactStore
  prompts/              # system.md + image_workflow.md
  providers/bytedance/  # 图像供应商适配
  tools/                # generate_images / analyze_image
  skills/               # image-workflow SKILL.md
```

## 模式选择判据

一个新 domain 该用哪种模式？

- **需要进化优化**（prompt/middleware 会迭代调优、要做 A/B 实验）→ 进化型。
- **功能固定**（工具型 domain，装配逻辑基本不变）→ 独立型。
- **不确定** → 先独立型（简单），后续需要进化时再包化。

## 与 platform 的边界

| 归 platform/ | 归 domains/ |
|-------------|-------------|
| SSE 骨架（run_agent_stream） | 领域 EventSink（WritingEventSink） |
| Agent 运行时（create_deep_agent 隔离层） | agent 装配（RuntimeContext 构建 或 包内 assemble） |
| 通用 middleware（TraceMiddleware） | 领域专属 middleware（如 PathGuard 的白名单配置） |
| 基类服务（BaseAgentService） | 领域服务（继承基类，注入领域差异） |
| trace/state/auth/core 基础设施 | 领域数据模型、领域算法、领域 LLM |
