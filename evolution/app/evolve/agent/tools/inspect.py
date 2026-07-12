"""探查工具（只读，4 个）——给进化 Agent 要素认知能力（决策 S2/S8）。

工具：
  - list_elements()          列出 harness 包八要素的当前文件清单
  - read_source(path)        读任意要素源码全文
  - inspect_state_schema()   查 DeepAgent State 字段结构（硬编码文档）
  - read_assemble()          读 assemble() 装配入口源码

这些工具让 Agent 动态探查"当前包里实际有什么"——system prompt 里的全景
是抽象知识，实际文件清单是动态的，靠这些工具按需查。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from app.core.settings import settings

logger = logging.getLogger("evolution.evolve.agent.tools.inspect")


def make_inspect_tools() -> list:
    """构建探查工具集（4 个，只读）。"""

    @tool
    def list_elements() -> str:
        """列出 harness 包内所有要素的文件清单（按目录归类）。

        返回 harnesses/current/ 下的目录树，让你快速了解包里有哪些要素文件：
        - prompts/    提示词（可改）
        - middleware/ 中间件（可改）
        - tools/      工具定义（可改）
        - subagents/  子代理定义（可改）
        - skills/     技能包（可改）
        - __init__.py assemble 装配入口（只读）
        """
        pkg_dir = settings.harness_work_dir_path
        if not pkg_dir.is_dir():
            return f"错误：harness 包目录不存在 {pkg_dir}"

        lines: list[str] = ["## harness 包文件清单", ""]
        # 按一级目录归类
        entries = sorted(pkg_dir.iterdir(), key=lambda p: (p.is_file(), p.name))
        for entry in entries:
            if entry.name.startswith("__pycache__") or entry.name == ".git":
                continue
            if entry.is_dir():
                files = [
                    p for p in entry.rglob("*")
                    if p.is_file()
                    and "__pycache__" not in p.parts
                    and ".git" not in p.parts
                ]
                lines.append(f"### {entry.name}/（{len(files)} 个文件）")
                for f in sorted(files)[:15]:
                    rel = f.relative_to(entry)
                    lines.append(f"  {rel}")
                if len(files) > 15:
                    lines.append(f"  …还有 {len(files) - 15} 个")
                lines.append("")
            elif entry.is_file():
                size = entry.stat().st_size
                lines.append(f"### {entry.name}（{size} 字节）")

        return "\n".join(lines)

    @tool
    def read_source(file_path: str) -> str:
        """读取 harness 包内某个源码文件的全文。

        用于查看要素的具体实现（middleware 逻辑、prompt 内容、tool 定义等）。
        file_path 是相对 harness 包根的路径（如 "middleware/goal.py"、"prompts/writing_system.md"）。
        可从 list_elements 的文件清单获取可用路径。

        Args:
            file_path: 相对 harness 包根的源码文件路径
        """
        pkg_dir = settings.harness_work_dir_path
        # 安全：resolve 后检查不越界
        full = (pkg_dir / file_path).resolve()
        try:
            full.relative_to(pkg_dir.resolve())
        except ValueError:
            return f"错误：路径越界（{file_path} 不在 harness 包内）"

        if not full.is_file():
            return f"错误：文件不存在 {file_path}"

        try:
            content = full.read_text(encoding="utf-8")
            return f"## {file_path}（{len(content)} 字符）\n\n```\n{content}\n```"
        except Exception as e:
            return f"读取失败 {file_path}：{e}"

    @tool
    def inspect_state_schema() -> str:
        """查看 DeepAgent State 的字段结构（只读 schema）。

        State 是 DeepAgent 框架中承载运行时信息的对象，由各 Middleware 扩展字段。
        **State 字段不能直接修改**——操作 State 的唯一合法途径是定义 Middleware
        （通过 hook 返回 dict 或工具返回 Command(update={...})）。

        本工具返回 State 各字段的说明：来源、承载什么、操作约束。
        """
        return _STATE_SCHEMA_DOC

    @tool
    def read_assemble() -> str:
        """读取 harness 包的装配入口 assemble() 的源码全文。

        assemble(ctx) 是 executor 与包的唯一交互点——它读 prompts、实例化 middleware、
        组装 subagent、调 create_deep_agent 返回完整 agent。
        读它能理解各要素怎么被装配到一起的。
        """
        pkg_dir = settings.harness_work_dir_path
        assemble_path = pkg_dir / "__init__.py"
        if not assemble_path.is_file():
            return "错误：assemble 入口文件不存在"

        try:
            content = assemble_path.read_text(encoding="utf-8")
            return f"## assemble() 装配入口（{len(content)} 字符）\n\n```python\n{content}\n```"
        except Exception as e:
            return f"读取 assemble 失败：{e}"

    return [list_elements, read_source, inspect_state_schema, read_assemble]


# ── State schema 硬编码文档（S8）──────────────────────────────────

_STATE_SCHEMA_DOC = """\
## DeepAgent State 字段结构

State 是一个分层 TypedDict，各 Middleware 扩展自己的字段。字段标记为
PrivateStateAttr 的不暴露给 LLM input/output schema（纯内部状态）。

### 基类字段（AgentState，所有 agent 都有）
- **messages**（Required）：对话消息列表。承载整个对话历史（Human/AI/Tool 消息）。
  reducer = DeltaChannel（每 50 步快照一次，控 checkpoint 增长）。
  → 经 Middleware 的 hook 返回 dict 操作（如注入 SystemMessage）。
- **jump_to**（私有）：流转控制。取值 "tools"|"model"|"end"|None。
  → Middleware 用 `{"jump_to": "model"}` 把流程跳回模型重跑。
- **structured_response**：结构化输出（配合 response_format）。

### TodoListMiddleware 扩展字段
- **todos**：任务清单。`list[{content: str, status: "pending"|"in_progress"|"completed"}]`。
  → 通过 write_todos 工具操作（返回 Command(update={"todos": ...})）。

### FilesystemMiddleware 扩展字段
- **files**（私有）：虚拟文件系统内容。`dict[路径, FileData]`。
  → 框架自带 fs 工具（read_file/write_file/edit_file）操作，但已被 NoFilesystemToolsMiddleware 禁用。

### MemoryMiddleware 扩展字段
- **memory_contents**（私有）：AGENTS.md 内容缓存。`dict[路径, 内容]`。

### GoalMiddleware 扩展字段（项目自定义，harness 包内）
- **goal**：当前目标声明。
- **goal_completed**：目标是否完成（bool|None）。
- **goal_acceptance_evidence**：目标完成的验收证据。
- **goal_output_blocked**：输出是否被拦截（未达标时不让输出）。
- **goal_output_block_count**：输出拦截次数。
  → 通过 set_goal / record_goal_completion 工具操作（返回 Command(update={...})）。

### 操作约束（铁律）
- **不能直接改 State 字段**——State 由 LangGraph 的 reducer 管理。
- **操作 State 的唯一合法途径 = Middleware**：
  1. Middleware 的 hook（before_model/after_model/wrap_tool_call 等）返回 `dict`，由 reducer 合并进 State。
  2. 工具函数返回 `Command(update={...})`，等价于 hook 返回 dict。
  3. Middleware 的 wrap_model_call/wrap_tool_call 用 `request.override(...)` 改请求（不改 State）。
- 如果你要操作 State 的某个字段（如加一个新字段、改 todos 逻辑），产出物是一个
  **新的 Middleware 定义**（write_middleware 写源码），而不是直接改字段。
"""


__all__ = ["make_inspect_tools"]
