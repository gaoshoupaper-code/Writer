---
type: design
status: draft
created: 2026-06-09
source: 基于 20260609_200000_incremental-outline-expansion.md 需求文档的详细技术设计
related:
  - backend/app/writer/expert_agent/agents/character.py
  - backend/app/writer/expert_agent/agents/outline.py
  - backend/app/writer/meta/agent.py
  - backend/app/writer/expert_agent/factory.py
---

# Storybuilding Agent 详细技术设计

## 1. 概述

### 1.1 目标

用一个统一的 `storybuilding` expert agent 替代现有的 `character.py` + `outline.py` 两个 agent，实现"增量式大纲拓展"能力。

**核心变化**：从"先生成角色，再串行生成大纲"的线性管道 → "每轮同时产出/扩展人物、故事线、世界观、总纲、卷纲"的迭代扩展。

### 1.2 替换边界

| 被替换 | 不变 |
|--------|------|
| `agents/character.py` | `agents/detail_outline.py` |
| `agents/outline.py` | `agents/writing.py` |
| `evaluators/character.py` | `evaluators/detail_outline.py` |
| `evaluators/outline.py` | `evaluators/writing.py` |
| `prompts/character_system.md` | `prompts/detail_outline_system.md` |
| `prompts/outline_system.md` | `prompts/writing_system.md` |
| `prompts/character_evaluation.md` | `prompts/detail_outline_evaluation.md` |
| `prompts/outline_evaluation.md` | `prompts/writing_evaluation.md` |
| `skills/character-creation/SKILL.md` | `skills/detail-planning/SKILL.md` |
| `skills/outline-generation/SKILL.md` | `skills/chapter-writing/SKILL.md` |

`detail_outline.py` 和 `writing.py` 不变，但需要适配新的 workspace 产物结构（读取 `storyline/`、`worldview.md`、`volume/` 等）。

---

## 2. 新的 Workspace 产物结构

```
{workspace}/
├── demand.md             # 用户需求（meta agent 维护，不变）
├── character/            # 人物档案，一个人物一个文件
│   ├── {角色名}.md
│   └── ...
├── storyline/            # 故事线，一条线一个文件
│   ├── {线名}.md
│   └── ...
├── worldview.md          # 世界观（单文件，各要素高度耦合）
├── outline.md            # 总纲（结构蓝图）
├── volume/               # 卷纲
│   ├── volume-01.md
│   ├── volume-02.md
│   └── ...
├── evaluation.md         # 统一评估报告（每轮覆盖写入）
├── detail/               # 细纲（detail_outline agent 写入，不变）
│   ├── overview.md
│   └── chapter-XX.md
├── chapter/              # 正文（writing agent 写入，不变）
└── review/               # 审查（writing agent 写入，不变）
```

### 2.1 与旧结构的对照

| 旧结构 | 新结构 | 说明 |
|--------|--------|------|
| `character/*.md` | `character/*.md` | 不变 |
| `outline.md`（含主线/次线/交织点） | `outline.md`（纯结构蓝图） | 主线/次线拆出到 storyline/ |
| `evaluation.md` | `evaluation.md` | 不变，但评估维度更新 |
| — | `storyline/*.md` | **新增**：独立故事线实体 |
| — | `worldview.md` | **新增**：独立世界观实体 |
| — | `volume/*.md` | **新增**：卷纲 |

### 2.2 向后兼容处理

`detail_outline` 和 `writing` 需要能读取新的产物结构。具体适配点：

- `detail_outline` 的 `context_file_paths` 需增加 `storyline/*.md`、`worldview.md`、`volume/*.md`
- `writing` 的 `context_file_paths` 同理
- `detail_outline` 的前置依赖检查需要适配新结构（见 §6）

---

## 3. Agent 架构设计

### 3.1 总体结构

```
MetaAgent (create_deep_agent)
  |-- general-purpose (SubAgent)
  |-- storybuilding (create_deep_agent)        ← 替代 character + outline
  |     |-- evolution (SubAgent)               ← 统一评估
  |-- detail-outline (create_deep_agent)       ← 不变
  |     |-- evolution
  |-- writing (create_deep_agent)              ← 不变
        |-- evolution
```

### 3.2 核心设计原则

1. **单 Agent 多维度**：一个 `storybuilding` agent 负责 5 个维度（人物、故事线、世界观、总纲、卷纲），不是 5 个独立 agent。
   - 原因：5 个维度高度耦合（人物驱动故事线，世界观支撑冲突，总纲编排交织），拆成独立 agent 会导致跨维度一致性维护极其困难。
   - 风险：单次调用的 token 消耗会增大。
   - 缓解：prompt 中明确指导 agent 优先处理委托指定的维度，未指定时自主判断。

2. **统一评估**：一个 evolution sub-agent 评估所有维度的跨维度一致性。

3. **迭代扩展**：meta agent 外循环控制迭代轮数，每轮调用 storybuilding 一次。

---

## 4. 新增文件清单

### 4.1 新增文件

| 文件路径 | 用途 |
|----------|------|
| `expert_agent/agents/storybuilding.py` | storybuilding agent 构建器 |
| `expert_agent/evaluators/storybuilding.py` | 统一评估构建器 |
| `expert_agent/prompts/storybuilding_system.md` | storybuilding 系统提示词 |
| `expert_agent/prompts/storybuilding_evaluation.md` | 统一评估系统提示词 |
| `expert_agent/skills/storybuilding/SKILL.md` | storybuilding 工作流 SOP |

### 4.2 修改文件

| 文件路径 | 修改内容 |
|----------|----------|
| `expert_agent/agents/__init__.py` | 移除 character/outline 导出，添加 storybuilding 导出 |
| `expert_agent/evaluators/__init__.py` | 移除 character/outline 导出，添加 storybuilding 导出 |
| `meta/agent.py` | 移除 character/outline 工厂方法，添加 storybuilding 工厂方法；更新 `_agent_for_workspace`、`_artifact_prerequisites_for_pipeline_subagent`、`_build_user_prompt` |
| `meta/prompts/system.md` | 重写子代理调用流程，反映新的迭代模式 |

### 4.3 删除文件（实现阶段清理，不阻塞新功能）

| 文件路径 | 说明 |
|----------|------|
| `expert_agent/agents/character.py` | 被 storybuilding 替代 |
| `expert_agent/agents/outline.py` | 被 storybuilding 替代 |
| `expert_agent/evaluators/character.py` | 被 storybuilding evaluator 替代 |
| `expert_agent/evaluators/outline.py` | 被 storybuilding evaluator 替代 |
| `expert_agent/prompts/character_system.md` | 不再需要 |
| `expert_agent/prompts/character_evaluation.md` | 不再需要 |
| `expert_agent/prompts/outline_system.md` | 不再需要 |
| `expert_agent/prompts/outline_evaluation.md` | 不再需要 |
| `expert_agent/skills/character-creation/SKILL.md` | 不再需要 |
| `expert_agent/skills/outline-generation/SKILL.md` | 不再需要 |

---

## 5. 详细设计

### 5.1 `agents/storybuilding.py`

遵循现有 agent 的双函数模式：`build_storybuilding_subagent()` + `build_storybuilding_deep_subagent()`。

```python
"""Storybuilding 子代理 — 增量式故事构建（人物+故事线+世界观+总纲+卷纲）。

替代原 character.py + outline.py，用单一 agent 统一管理五个创作维度。
"""
from __future__ import annotations

from pathlib import Path

from deepagents import CompiledSubAgent, SubAgent
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.expert_agent.factory import build_deep_subagent
from app.writer.expert_agent.evaluators.storybuilding import build_storybuilding_evaluator
from app.writer.expert_agent.types import SubAgentSpec, apply_style_suffix
from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "storybuilding_system.md"


def build_storybuilding_subagent(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    style_suffix: str | None = None,
) -> SubAgent:
    """构建 storybuilding 子代理规格。

    写入权限覆盖 5 个维度：character/, storyline/, worldview.md, outline.md, volume/
    """
    system_prompt = apply_style_suffix(
        PROMPT_PATH.read_text(encoding="utf-8").strip(),
        style_suffix,
    )

    permissions = [
        # 读取：允许读取所有文件
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        # 写入：允许写入 5 个维度 + evaluation.md
        FilesystemPermission(operations=["write"], paths=["/character/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/storyline/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/worldview.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/outline.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/volume/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/evaluation.md"], mode="allow"),
        # 拒绝：禁止写入其他文件
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]

    spec = SubAgent(
        name="storybuilding",
        description=(
            "适用：需要构建或扩展小说故事世界时调用——包括人物、故事线、世界观、总纲、卷纲。"
            "支持增量迭代：每轮基于已有产物扩展，同时维护跨维度一致性。"
            "内置统一评估：每轮产出后自动评估所有维度的跨维度一致性和完整性。"
            "委托时必须说明：当前轮次、用户扩展方向（如有）、本轮焦点维度。"
        ),
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_storybuilding_deep_subagent(
    workspace_root: Path,
    model: object,
    backend: object,
    middleware_factory: "collections.abc.Callable[[str], list[AgentMiddleware]]",
    style_suffix: str | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 storybuilding 子代理（含统一评估循环）。

    调用流程：
      1. meta agent 委托，传入轮次信息和扩展方向
      2. storybuilding 读取已有产物，扩展指定维度
      3. 调用 evolution 统一评估
      4. 评估建议修订时自动修订（最多 3 轮）
      5. 返回汇总结果
    """
    # ---- 主代理 middleware ----
    storybuilding_middleware = list(middleware_factory("storybuilding-subagent"))
    # 注入已有产物作为上下文
    storybuilding_middleware.append(
        ContextAssemblerMiddleware(
            workspace_root,
            file_paths=[
                "character/*.md",
                "storyline/*.md",
                "worldview.md",
                "outline.md",
                "volume/*.md",
            ],
            context_label="已有故事产物",
        )
    )
    primary_spec = build_storybuilding_subagent(
        workspace_root, storybuilding_middleware, style_suffix
    )

    # ---- 统一评估子代理 ----
    evaluation_spec = build_storybuilding_evaluator(
        workspace_root,
        middleware_factory("storybuilding-evaluation-subagent"),
        context_file_paths=[
            "character/*.md",
            "storyline/*.md",
            "worldview.md",
            "outline.md",
            "volume/*.md",
        ],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description=(
            "统一评估所有故事维度（人物、故事线、世界观、总纲、卷纲）的跨维度一致性。"
            "读取所有产物，写入 evaluation.md，返回评分和修订建议。"
        ),
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- Skill 路径 ----
    skills_path = str(Path(__file__).resolve().parent.parent / "skills")

    # ---- 调用工厂 ----
    # 注意：artifact_paths 包含 outline.md 确保至少写了总纲
    return build_deep_subagent(
        name="storybuilding",
        description=(
            "适用：需要构建或扩展小说故事世界时调用——包括人物、故事线、世界观、总纲、卷纲。"
            "支持增量迭代：每轮基于已有产物扩展，同时维护跨维度一致性。"
            "内置统一评估：每轮产出后自动评估所有维度的跨维度一致性和完整性，最多 3 轮修订。"
            "委托时必须说明：当前轮次、用户扩展方向（如有）、本轮焦点维度。"
        ),
        model=model,
        system_prompt=primary_spec["system_prompt"],
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        artifact_paths=[workspace_root / "outline.md"],
        max_revisions=3,
        skills=[skills_path],
    )
```

**设计要点说明**：

1. **写入权限分维度声明**：5 个维度的写入路径分别声明，便于审查和维护。`evaluation.md` 也需要写入权限（但实际由 evolution sub-agent 写入，主 agent 不需要——这里声明是冗余但无害的，因为 evolution 作为子 agent 有自己的权限声明）。

2. **ContextAssemblerMiddleware 注入已有产物**：每次调用前自动注入 `character/*.md`、`storyline/*.md`、`worldview.md`、`outline.md`、`volume/*.md`。第一轮这些文件不存在时中间件会优雅处理（不注入）。

3. **artifact_paths 只验证 outline.md**：确保总纲至少被写入。其他维度的产物验证由评估 agent 负责，不需要硬性验证。

4. **双函数模式与现有 agent 一致**：`build_storybuilding_subagent()` 返回裸规格，`build_storybuilding_deep_subagent()` 包装为 DeepAgent。这保持了与现有架构的一致性。

### 5.2 `evaluators/storybuilding.py`

统一评估构建器，遵循现有 evaluator 模式：

```python
"""Storybuilding 统一评估子代理构建器。

评估所有故事维度的跨维度一致性，写入 evaluation.md。
"""
from __future__ import annotations

from pathlib import Path

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.expert_agent.types import SubAgentSpec

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "storybuilding_evaluation.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/evaluation.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_storybuilding_evaluator(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    context_file_paths: list[str] | None = None,
) -> SubAgentSpec:
    """构建统一评估子代理规格。

    Args:
        workspace_root:     工作区根目录
        middleware:         额外中间件（可选）
        context_file_paths: 需要注入为评估上下文的文件路径模式列表

    Returns:
        评估子代理规格字典
    """
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()

    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS)

    eval_middleware: list[AgentMiddleware] = []
    if context_file_paths:
        eval_middleware.append(
            ContextAssemblerMiddleware(
                workspace_root,
                file_paths=context_file_paths,
                context_label="评估前置上下文：所有故事维度产物",
            )
        )
    if middleware is not None:
        eval_middleware.extend(middleware)

    return SubAgentSpec(
        name="evaluation-storybuilding",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=eval_middleware,
    )
```

### 5.3 `agents/__init__.py` 和 `evaluators/__init__.py` 变更

**agents/__init__.py**：
```python
"""expert_agent.agents -- all creative sub-agent build functions."""
from app.writer.expert_agent.agents.storybuilding import (
    build_storybuilding_subagent,
    build_storybuilding_deep_subagent,
)
from app.writer.expert_agent.agents.detail_outline import (
    build_detail_outline_subagent,
    build_detail_outline_deep_subagent,
)
from app.writer.expert_agent.agents.writing import (
    build_writing_subagent,
    build_writing_deep_subagent,
)

__all__ = [
    "build_storybuilding_subagent",
    "build_storybuilding_deep_subagent",
    "build_detail_outline_subagent",
    "build_detail_outline_deep_subagent",
    "build_writing_subagent",
    "build_writing_deep_subagent",
]
```

**evaluators/__init__.py**：
```python
"""expert_agent.evaluators -- all evaluation sub-agent build functions."""
from app.writer.expert_agent.evaluators.storybuilding import build_storybuilding_evaluator
from app.writer.expert_agent.evaluators.detail_outline import build_detail_outline_evaluator
from app.writer.expert_agent.evaluators.writing import build_writing_evaluator

__all__ = [
    "build_storybuilding_evaluator",
    "build_detail_outline_evaluator",
    "build_writing_evaluator",
]
```

### 5.4 `meta/agent.py` 变更

#### 5.4.1 移除的方法

- `_character_subagent_for_workspace()`
- `_outline_subagent_for_workspace()`

#### 5.4.2 新增的方法

```python
def _storybuilding_subagent_for_workspace(
    self,
    workspace_path: Path,
    trace_id: str | None = None,
    style_suffix: str | None = None,
) -> CompiledSubAgent:
    """构建 storybuilding 子代理（含统一评估循环）。"""
    return build_storybuilding_deep_subagent(
        workspace_path,
        build_writer_model(self.settings),
        self._backend_for_workspace(workspace_path),
        lambda agent_name: self._middleware_for_workspace(workspace_path, trace_id, agent_name),
        style_suffix=style_suffix,
    )
```

#### 5.4.3 修改 `_agent_for_workspace()`

```python
def _agent_for_workspace(self, workspace_path: Path, trace_id: str | None = None, workspace_id: str | None = None):
    model = build_writer_model(self.settings)
    middleware: list[AgentMiddleware] = [
        GoalMiddleware(),
        ErrorRecoveryMiddleware(),
        FilesystemPathGuardMiddleware(workspace_path, allowed_write_paths=("/demand.md",)),
    ]
    if trace_id:
        middleware.insert(1, TraceMiddleware(self.trace_recorder, trace_id, "meta-agent"))
    meta_style = self._resolve_meta_style(workspace_id) if workspace_id else None
    storybuilding_style = self._resolve_style_for_subagent(workspace_id, "storybuilding_style") if workspace_id else None
    detail_outline_style = self._resolve_style_for_subagent(workspace_id, "detail_outline_style") if workspace_id else None
    writing_style = self._resolve_style_for_subagent(workspace_id, "writing_style") if workspace_id else None
    return create_deep_agent(
        model=model,
        tools=[],
        system_prompt=self._load_system_prompt(meta_style),
        subagents=[
            self._general_subagent_for_workspace(workspace_path, trace_id),
            self._storybuilding_subagent_for_workspace(workspace_path, trace_id, storybuilding_style),
            self._detail_outline_subagent_for_workspace(workspace_path, trace_id, detail_outline_style),
            self._writing_subagent_for_workspace(workspace_path, trace_id, writing_style),
        ],
        backend=self._backend_for_workspace(workspace_path),
        checkpointer=self.checkpointer,
        middleware=middleware,
    )
```

**变更要点**：
- `character` + `outline` 两个子代理 → `storybuilding` 一个子代理
- style_key 从 `character_style` / `outline_style` → `storybuilding_style`（需同步更新 CreateTypeStore 的 style schema）
- 前置依赖检查简化（见 §5.4.4）

#### 5.4.4 修改 `_artifact_prerequisites_for_pipeline_subagent()`

```python
def _artifact_prerequisites_for_pipeline_subagent(
    self,
    workspace_path: Path,
    agent_name: str,
) -> list[ArtifactPrerequisite]:
    if agent_name == "detail-outline-subagent":
        return [
            ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True),
            ArtifactPrerequisite("story outline", workspace_path / "outline.md"),
            ArtifactPrerequisite("worldview", workspace_path / "worldview.md"),
            ArtifactPrerequisite("storylines", workspace_path / "storyline", markdown_directory=True),
        ]
    if agent_name == "writing-subagent":
        return [
            ArtifactPrerequisite("character design", workspace_path / "character", markdown_directory=True),
            ArtifactPrerequisite("story outline", workspace_path / "outline.md"),
            ArtifactPrerequisite("detail outline", workspace_path / "detail", markdown_directory=True),
        ]
    return []
```

**变更**：
- 移除 `outline-subagent` 的前置依赖（不再有独立的 outline agent）
- `detail-outline-subagent` 增加 `worldview.md` 和 `storyline/` 作为前置依赖
- `writing-subagent` 的前置依赖不变（它通过 context_file_paths 读取额外产物）

#### 5.4.5 修改 `_build_user_prompt()`

新的用户 prompt 需要指导 meta agent 使用迭代模式：

```python
def _build_user_prompt(self, payload: ScreenplayGenerateRequest, thread: ThreadSummary) -> str:
    # ... (context_lines 和 request_text 构建不变) ...

    return (
        "请根据用户需求执行创作任务。\n"
        "当前工作目录：/\n"
        f"当前 session：{thread.thread_id}\n\n"

        "## 故事构建流程\n\n"
        "对于需要完整故事构建的需求（长篇/中篇），采用迭代式构建：\n"
        "1. 判断故事规模，确定需要的迭代轮数（通常 2-4 轮）。\n"
        "2. 每轮委托 storybuilding 子代理，传入：当前轮次编号、用户扩展方向（如有）、本轮焦点。\n"
        "3. 每轮返回后检查评估结果；如标记质量风险，在下一轮优先修复。\n"
        "4. 循环结束后进入 detail-outline → writing 阶段。\n\n"

        "## storybuilding 委托规范\n\n"
        "每次委托 storybuilding 时必须明确传达：\n"
        "- 当前轮次（第几轮/共几轮）\n"
        "- 本轮焦点：优先扩展哪些维度（人物/故事线/世界观/总纲/卷纲）\n"
        "- 用户的扩展方向（如有）\n"
        "- 前几轮评估中发现的问题（如有）\n\n"

        "## 后续阶段\n\n"
        "storybuilding 迭代完成后，进入细纲和正文阶段：\n"
        "- detail-outline：需要读取 storyline/、worldview.md、volume/ 等新增产物。\n"
        "- writing：上下文由中间件自动注入。\n\n"

        # ... 其余不变 ...
    )
```

### 5.5 `meta/prompts/system.md` 变更

重写子代理调用流程部分，移除 character 和 outline 的独立说明，替换为 storybuilding：

```markdown
## 子代理调用流程

### 前置依赖
- 调用 storybuilding 前：无前置依赖（第一轮从零构建）。
- 调用 detail-outline 前：确认 character/ 已有角色文件、outline.md 存在非空、
  worldview.md 存在、storyline/ 有故事线文件。
- 调用 writing 前：确认 character/ 已有角色文件、outline.md 存在非空、
  detail/ 下有对应章节细纲。

### 各子代理说明

1. **storybuilding**：增量式故事构建，同时产出/扩展人物、故事线、世界观、总纲和卷纲。
   内置统一评估循环：每轮产出后自动评估所有维度的跨维度一致性，最多 3 轮修订。
   支持迭代扩展：meta agent 通过外循环多次调用，每次传入轮次和扩展方向。
   委托时必须说明：当前轮次编号、本轮焦点维度、用户扩展方向（如有）、前几轮问题（如有）。

2. **detail-outline**：（不变）

3. **writing**：（不变）
```

---

## 6. Prompt 设计

### 6.1 `storybuilding_system.md` — 核心结构

```markdown
你是一名专业的增量式故事构建助手。你同时负责五个创作维度：
人物（character）、故事线（storyline）、世界观（worldview）、总纲（outline）、卷纲（volume）。

## 文件写入规则

- 当前工作目录根目录可能存在 `demand.md`，它记录用户需求，由主代理维护；
  你只能读取它作为需求背景，不能写入、编辑或删除它。
- 所有产物写入指定路径，不要修改其他文件。

### 维度与文件路径

| 维度 | 文件路径 | 格式 |
|------|----------|------|
| 人物 | `character/{角色名}.md` | 一个人物一个文件 |
| 故事线 | `storyline/{线名}.md` | 一条线一个文件 |
| 世界观 | `worldview.md` | 单文件 |
| 总纲 | `outline.md` | 单文件 |
| 卷纲 | `volume/volume-XX.md` | 一卷一个文件 |

## 增量扩展原则

你收到的委托会包含：
- **当前轮次**：第几轮（首轮从零构建，后续轮次增量扩展）
- **本轮焦点**：优先处理哪些维度
- **用户扩展方向**：用户希望增加/修改的内容（如有）
- **前几轮问题**：评估发现的问题（如有）

### 首轮（第 1 轮）最小可用产出

首轮必须产出：
- 至少 1 个主角档案（写入 character/）
- 至少 1 条主线（写入 storyline/）
- 基础世界观框架（写入 worldview.md）：至少包含时代背景、核心冲突根源
- 总纲骨架（写入 outline.md）：故事核心 + 结构骨架 + 卷划分
- 初步卷纲（写入 volume/）：至少第一卷的基本结构

其他维度可以留占位符（标注"待后续轮次补充"）。

### 后续轮次

后续轮次基于已有产物增量扩展：
- 先读取所有已有文件，理解当前状态
- 根据本轮焦点和用户方向决定扩展内容
- 保持与已有内容的连贯性和一致性
- 不要从零重写已有文件，而是增量深化和补充

## 各维度内容规范

### 人物（character/{角色名}.md）

每个角色档案包含：
- 基本信息：姓名、年龄、性别、身份/职业
- 角色定位：主角/配角/反派/功能性角色；叙事结构功能（推动者/回应者/催化剂/对立面）
- 外貌特征：长相体态、穿着打扮、标志性细节（具体、有画面感）
- 性格特质：核心性格标签 + 行为标识（口头禅、习惯动作、决策模式）——用行为定义性格，不写心理描写
- 核心欲望与恐惧：Want（外在目标）vs Need（内在需求）；核心恐惧
- 角色弧光：起点状态 → 转变契机 → 终点状态（弧度与故事体量匹配）
- 能力与局限：擅长什么、不擅长什么、致命弱点
- 关系网络：与各角色的关系类型（亲疏/对立/依赖）+ 动态变化方向

### 故事线（storyline/{线名}.md）

每条故事线包含：
- 名称与类型：主线 / 次线 / 暗线
- 驱动角色与动机：谁在推动这条线，核心动机
- 核心冲突：这条线围绕的矛盾/张力
- 弧线走向：起点状态 → 终点状态（状态转变，不是情节展开）
- 关键转折点：结构性 beat——定义"结构位置和功能"，不写具体场景
- 与其他故事线的关系：依赖、对立、并行、交汇
- 功能定位：这条线为什么存在

### 世界观（worldview.md）

单文件，包含：
- 时代与背景
- 地理与空间
- 社会结构：阶层/阶级、权力分配、重要组织/势力
- 核心规则：世界运转的底层规则
- 文化与信仰
- 历史脉络：只写与故事有关的
- 冲突根源：这个世界的核心矛盾

设计原则：每个要素"与故事有关才写"，不写世界百科全书。

### 总纲（outline.md）

总纲是结构蓝图，不是情节展开：
- 故事核心：Logline、核心主题、类型与基调
- 结构骨架：结构类型 + 选择理由
- 节奏曲线：全篇张力分布
- 卷划分：分几卷、每卷的核心结构功能
- 故事线编排：各故事线在哪一卷交汇、交织方式
- 主题递进：主题在各阶段的深化方式

### 卷纲（volume/volume-XX.md）

每卷包含：
- 卷标题
- 卷定位：在整体弧线中的结构功能
- 该卷涉及的故事线
- 出场人物：本卷核心人物 + 进入/退出状态
- 关键转折与事件
- 上下卷衔接
- 该卷内部节奏

## 创作原则

- 使用中文撰写。
- 人物用行为定义性格，不写心理描写。
- 外貌描写要具体、有画面感。
- 总纲是结构蓝图，不写具体情节。
- 故事线关注弧线走向和因果关系，不展开具体场景。
- 所有维度必须能追溯到故事核心，游离内容应当删除。
- 维度之间保持一致性：人物的欲望驱动故事线，世界观支撑冲突根源，
  总纲编排各线的交织，卷纲是总纲的展开。

## evolution 评估循环

完成本轮所有产物写入后，进入 evolution 评估循环：

1. 调用 `evolution` 子代理统一评估所有维度。
2. evolution 读取所有产物，写入 `evaluation.md`，返回评分和修订建议。
3. 根据 evolution 返回结果：
   - 建议修订 → 读取 evaluation.md，修订对应产物，再次调用 evolution
   - 无需修改 → 直接返回
4. 最多调用 evolution 3 次（含首次），超过后系统强制终止。
5. 返回时包含：修订轮数、是否有质量风险。

## 回复格式

- 使用纯文本，不返回 JSON。
- 简要说明本轮扩展了哪些维度、主要变动和下一步建议。
- 不输出完整文件内容。
```

### 6.2 `storybuilding_evaluation.md` — 统一评估

```markdown
你是一名故事质量评审专家。你的职责是评估故事构建产物的跨维度一致性。

## 评估范围

读取以下所有产物，进行整体评估：
- `character/*.md`：人物档案
- `storyline/*.md`：故事线
- `worldview.md`：世界观
- `outline.md`：总纲
- `volume/*.md`：卷纲

## 评估维度（满分 100）

### 1. 人物-故事线匹配（20 分）

角色弧光与故事线弧线是否一致？核心欲望是否驱动核心冲突？
有无游离角色（在故事线中找不到功能）或空头故事线（没有角色驱动）？

### 2. 世界观-故事线支撑（15 分）

世界观能否支撑故事线涉及的力量体系/社会冲突？
有无故事线假设了不存在的世界元素？

### 3. 总纲编排合理性（20 分）

交织编排是否准确？节奏曲线是否匹配？卷划分是否合理？

### 4. 增量一致性（20 分）

新增与已有之间有无矛盾？关系网络、交汇点是否自洽？
（首轮此维度自动满分，因为不存在"已有"内容）

### 5. 覆盖度（15 分）

有无引用未定义的世界观元素？未创建的角色？
不存在的交织点？卷纲中引用了不存在的故事线？

### 6. 主题贯穿度（10 分）

所有故事线是否指向同一核心主题？有无游离线？

## 评分与结论

评分规则：
- 90+：无需修改，继续推进
- 75-89：建议修改，但可接受当前版本继续
- 60-74：必须修改，存在明显问题
- <60：暂停，需要重大修复

## 输出格式

将评估报告写入 `/evaluation.md`，格式如下：

```markdown
# 故事构建评估报告

## 总分：{分数}/100

## 结论：无需修改 / 建议修改 / 必须修改

## 各维度评分

| 维度 | 分值 | 说明 |
|------|------|------|
| 人物-故事线匹配 | {分数}/20 | ... |
| 世界观-故事线支撑 | {分数}/15 | ... |
| 总纲编排合理性 | {分数}/20 | ... |
| 增量一致性 | {分数}/20 | ... |
| 覆盖度 | {分数}/15 | ... |
| 主题贯穿度 | {分数}/10 | ... |

## 核心问题（如有）

1. 问题描述 + 影响 + 证据

## 修订建议（如有）

按优先级排列，每个建议说明：修改哪个维度、哪个文件、具体修改什么。
```

## 返回格式

向父代理返回时，包含：
- 总分
- 结论（无需修改 / 建议修改 / 必须修改）
- 核心问题摘要（如有）
```

### 6.3 `skills/storybuilding/SKILL.md`

```markdown
---
name: storybuilding
description: >-
  增量式故事构建执行流程。支持多轮迭代，每轮同时产出/扩展人物、故事线、
  世界观、总纲和卷纲。包含完整的工作流程步骤和统一评估循环的使用方法。
---

# storybuilding

增量式故事构建执行流程。

## 工作流程

### 步骤 1 — 读取需求背景

读取工作目录根目录下的 `demand.md`，了解用户需求和创作背景。
- `demand.md` 由主代理维护，你只能读取，不能写入、编辑或删除。

### 步骤 2 — 读取已有产物

读取所有已有产物文件：
- `character/*.md`（如存在）
- `storyline/*.md`（如存在）
- `worldview.md`（如存在）
- `outline.md`（如存在）
- `volume/*.md`（如存在）

识别当前构建状态：哪些维度已有内容、各自完整度如何。

### 步骤 3 — 确定本轮目标

根据委托信息确定本轮目标：
- **首轮**：构建最小可用产出（见系统提示词中的"首轮最小可用产出"）
- **后续轮次**：根据本轮焦点和用户方向，增量扩展对应维度
- **修复轮次**：如前几轮评估发现问题，优先修复

### 步骤 4 — 产出/扩展各维度

按照系统提示词中的内容规范，产出或扩展各维度内容。

写入顺序建议（后写的可以引用先写的）：
1. `worldview.md`（世界观是底层基础）
2. `character/*.md`（人物在世界观中活动）
3. `storyline/*.md`（故事线由人物驱动）
4. `outline.md`（总纲编排所有故事线）
5. `volume/*.md`（卷纲是总纲的展开）

### 步骤 5 — evolution 统一评估循环

完成所有产物写入后，进入评估循环：

1. 调用 `evolution` 子代理统一评估。
2. evolution 读取所有维度产物，写入 `evaluation.md`，返回评分和修订建议。
3. 根据返回结果：
   - 建议修订 → 读取 `evaluation.md`，修订对应产物，再次调用 evolution
   - 无需修改 → 直接返回
4. 最多调用 evolution 3 次（含首次）。
5. 返回时包含：修订轮数、是否有质量风险。

### 步骤 6 — 回复父代理

用纯文本简要汇报：
- 本轮扩展了哪些维度
- 主要变动概述
- 评估状态
- 下一步建议

## 注意事项

- 只写入 5 个维度对应的文件路径，不修改 demand.md 或其他文件。
- 增量扩展时先读取再修改，不要从零重写已有文件。
- 维度之间保持一致性：引用其他维度的内容时，确保被引用的内容确实存在。
```

---

## 7. 下游适配

### 7.1 `detail_outline` 适配

`detail_outline` agent 的 `context_file_paths` 需要增加新的产物路径：

**变更位置**：`meta/agent.py` 的 `_detail_outline_subagent_for_workspace()`

```python
def _detail_outline_subagent_for_workspace(self, workspace_path, trace_id=None, style_suffix=None):
    return build_detail_outline_deep_subagent(
        workspace_path,
        build_writer_model(self.settings),
        self._backend_for_workspace(workspace_path),
        lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
        style_suffix=style_suffix,
        context_file_paths=[
            "outline.md",
            "character/*.md",
            "storyline/*.md",       # 新增
            "worldview.md",          # 新增
            "volume/*.md",           # 新增
            "detail/overview.md",
            "detail/chapter-*.md",
        ],
    )
```

### 7.2 `writing` 适配

同理增加新的上下文路径：

```python
def _writing_subagent_for_workspace(self, workspace_path, trace_id=None, style_suffix=None):
    return build_writing_deep_subagent(
        workspace_path,
        build_writer_model(self.settings),
        self._backend_for_workspace(workspace_path),
        lambda agent_name: self._middleware_for_pipeline_subagent(workspace_path, trace_id, agent_name),
        style_suffix=style_suffix,
        context_file_paths=[
            "outline.md",
            "character/*.md",
            "storyline/*.md",       # 新增
            "worldview.md",          # 新增
            "volume/*.md",           # 新增
            "detail/*.md",
        ],
    )
```

### 7.3 Style Schema 适配

`CreateTypeStore` 的 style schema 需要合并 `character_style` + `outline_style` 为 `storybuilding_style`。

**影响范围**：`app/create_type/store.py` 和前端 style 配置 UI（如果有的话）。

---

## 8. 实现顺序

按依赖关系排序，每一步有明确的验证点：

### Phase 1：新增 storybuilding agent（不破坏现有功能）

| 步骤 | 操作 | 验证 |
|------|------|------|
| 1.1 | 创建 `prompts/storybuilding_system.md` | 内容完整、格式正确 |
| 1.2 | 创建 `prompts/storybuilding_evaluation.md` | 评估维度和输出格式正确 |
| 1.3 | 创建 `skills/storybuilding/SKILL.md` | 流程步骤完整 |
| 1.4 | 创建 `evaluators/storybuilding.py` | 可导入、返回正确的 SubAgentSpec |
| 1.5 | 创建 `agents/storybuilding.py` | 可导入、返回正确的 CompiledSubAgent |

### Phase 2：集成到 meta agent（替换旧 agent）

| 步骤 | 操作 | 验证 |
|------|------|------|
| 2.1 | 更新 `agents/__init__.py` 和 `evaluators/__init__.py` | 导入无报错 |
| 2.2 | 修改 `meta/agent.py`：添加 `_storybuilding_subagent_for_workspace()` | 函数签名正确 |
| 2.3 | 修改 `meta/agent.py`：更新 `_agent_for_workspace()` | 使用 storybuilding 替代 character + outline |
| 2.4 | 修改 `meta/agent.py`：更新 `_build_user_prompt()` | prompt 引导正确 |
| 2.5 | 更新 `meta/prompts/system.md` | 子代理描述正确 |

### Phase 3：下游适配

| 步骤 | 操作 | 验证 |
|------|------|------|
| 3.1 | 更新 `_artifact_prerequisites_for_pipeline_subagent()` | detail-outline 前置依赖正确 |
| 3.2 | 更新 `_detail_outline_subagent_for_workspace()` context_file_paths | 包含新产物路径 |
| 3.3 | 更新 `_writing_subagent_for_workspace()` context_file_paths | 包含新产物路径 |

### Phase 4：清理旧文件

| 步骤 | 操作 | 验证 |
|------|------|------|
| 4.1 | 删除旧 agent 和 evaluator 文件 | 无残留引用 |
| 4.2 | 删除旧 prompt 和 skill 文件 | 无残留引用 |
| 4.3 | 全量导入测试 | `python -c "from app.writer.meta import MetaAgentService"` 成功 |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 单 agent 处理 5 个维度，单次调用 token 消耗大 | 成本增加、可能超时 | prompt 引导按焦点维度优先处理；首轮最小产出原则 |
| 旧 workspace 数据不兼容 | 已有项目无法使用新 agent | workspace 结构是增量的（新增 storyline/、worldview.md、volume/），不破坏旧结构 |
| evaluation.md 路径冲突 | 旧 outline evaluator 写 `/evaluation.md`，新 evaluator 也写同路径 | 功能上正确（覆盖写入），但语义不同；新评估报告内容更全面 |
| ContextAssemblerMiddleware 在首轮文件不存在时的行为 | 首轮无文件可注入 | 中间件已处理空文件列表（优雅跳过） |
| CreateTypeStore style schema 变更影响前端 | 前端 style 配置需要适配 | 需要同步更新前端代码，或做向后兼容（同时支持旧 key 和新 key） |
