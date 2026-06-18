# 文生图优化 Agent + Skills 自进化系统

基于冻结的需求基准（D1-D21）和设计基准（DD1-DD10）实现的完整系统。

## 架构（DD1）

```
backend/app/
├── platform/              # 领域无关共享基座
│   ├── agent/
│   │   ├── base_service.py    # BaseAgentService（model/checkpointer 解析、checkpoint 读写）
│   │   ├── hitl.py            # HITL interrupt payload 统一协议（DD4：kind 路由）
│   │   └── middleware/        # 中间件位（Phase 2 物理迁移预留）
│   ├── providers/
│   │   ├── image_generation.py    # ImageGenerationProvider Protocol（DD8c）
│   │   └── image_understanding.py # ImageUnderstandingProvider Protocol（DD8c）
│   ├── skills/
│   │   └── loader.py          # resolve_owner_skills（按 owner 加载私有 SKILL.md，DD7b）
│   ├── core/ auth/ workspace/  # 位预留（Phase 2 物理迁移）
├── domains/
│   └── image/             # 文生图能力域
│       ├── agent.py           # ImageAgentService（继承 BaseAgentService，DD3）
│       ├── store.py           # ImageArtifactStore（图片落盘 + provider 解析）
│       ├── tools/             # generate_images / analyze_image / persist_skill（DD4/D5/D8）
│       ├── providers/bytedance/  # 字节生图/视觉 API（D3/D14 占位 mock）
│       ├── prompts/           # image-agent 系统提示词 + image-workflow SKILL.md
│       ├── router.py          # REST：图片端点 + Skill 管理（DD8b/D18）
│       └── skills/image-workflow/SKILL.md  # 闭环流程 Skill
├── writer/                # 写作 domain（现有，MetaAgentService 已继承 BaseAgentService）
└── main.py                # FastAPI 组装点（platform + writing + image）
```

## 核心闭环（D4/D5/D6）

```
用户需求
  → image-agent 优化 3 版提示词（D21：Agent 自主定方向）
  → generate_images 工具：3 版 × 双采样 = 6 张图（D4）
  → analyze_image 工具：视觉自评（D5 第一层：质量+匹配度，D14）
  → ask_user 触发 HITL interrupt（kind=image_review）
  → 前端 ImageReviewCard 渲染 3×2 网格 + 1-5 星 + 文本框（D12/D13）
  → 用户打分反馈（resume 结构化对象，DD4）
  → Agent 据反馈迭代（action=continue）或收尾（action=stop）
  → 收尾时问是否持久化成 Skill（D8），同意则 persist_skill
```

## Skills 自进化系统（D2/D7-D9/D16/D18）

- **存储**：`backend/skills/<owner_id>/<skill_id>/SKILL.md`（owner 级，跨 workspace 复用）
- **元数据**：skills 表（name/scene_tag/description/revision_count）
- **加载**：`resolve_owner_skills(owner_id, selected_skill_ids)` 按 owner 选中加载（D9）
- **进化**：`persist_skill` 工具双写（文件 + DB），revision_count 自增
- **冷启动**：纯冷启动，无种子（D20）
- **管理**：`/skills` 页面（查看/重命名/删除/编辑，D18）

## 数据模型（DD7）

- `images` 表：单表，字段内嵌评估数据（agent_analysis/user_score/user_note，DD7a）
- `skills` 表：Skill 元数据（DD7b）
- `workspaces` 表：加 `domain` 字段 + `title`（原 outline_name，DD2）

## 外部 API（D3/D14 占位）

字节生图/视觉 API 当前为 mock（`domains/image/providers/bytedance/`）：
- `BytedanceImageProvider`：按 prompt+seed 生成确定性纯色 PNG
- `BytedanceVisionProvider`：返回占位分析

真实 API 接入后替换方法体即可（接口已抽象，DD8c）。

## REST 端点

- `POST /api/image/generate/stream`：文生图 SSE 流
- `GET /api/images/{image_id}`：图片服务端点（DD8b）
- `GET/PUT/DELETE /api/skills[/{id}]`：Skill 管理（D18）
- `POST /api/skills/merge`：Skill 合并（D18c）

## 测试

```bash
cd backend
python -m pytest tests/test_image_domain.py -v  # image domain（13 测试）
python -m pytest tests/ -q                       # 全量（105 通过）
```

## 关键设计决策索引

| 决策 | 来源 | 文件 |
|---|---|---|
| DD1 平台解耦 | platform/ + domains/ | 目录结构 |
| DD2 workspace domain | db/__init__.py | workspaces 表 |
| DD3 DeepAgent+工具 | domains/image/agent.py | ImageAgentService |
| DD4 HITL 协议 | platform/agent/hitl.py | kind 路由 |
| DD5 两层反馈 | tools/ + ImageReviewCard | 自评+用户评 |
| DD6 用户喊停 | prompts/system.md | action=stop |
| DD7 数据模型 | db/__init__.py | images/skills 表 |
| DD8 Provider 抽象 | platform/providers/ | 双 Protocol |
| PathGuard 参数化 | writer/middleware/path_guard | allowed_patterns |
| BaseAgentService | platform/agent/base_service | 模板方法基类 |
