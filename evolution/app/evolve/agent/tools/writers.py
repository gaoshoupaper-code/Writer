"""写工具（5 写 + 1 edit）——给进化 Agent 受控的要素修改能力（决策 S5/S10）。

每个可改要素配一个专用写工具（封装路径锁定 + backend 落盘），
另外 1 个 edit_source 工具用于修改已有文件。

设计原则（S5）：
  - write_* 仅新建（backend.write，已存在报错 → 提示用 edit_source）
  - edit_source 修改已有（backend.edit，精确替换 old_string→new_string）
  - 所有写操作经 FilesystemBackend（virtual_mode 路径安全 + symlink 防护）
  - name/path 参数 sanitize（防路径穿越），backend 再做一层拦截

要素目录映射：
  write_prompt     → /prompts/{name}.md
  write_middleware → /middleware/{name}.py
  write_tool       → /tools/{name}.py
  write_skill      → /skills/{path}
  write_subagent   → /subagents/{name}.py
  edit_source      → 任意已有文件（path + old_string + new_string）
"""
from __future__ import annotations

import logging
import re

from langchain_core.tools import tool

from app.evolve.ctx import get_tool_context

logger = logging.getLogger("evolution.evolve.agent.tools.writers")

# 合法文件名：字母数字下划线连字符，不允许路径分隔符 / ..
# 防止 Agent 传 "../etc/passwd" 或 "a/b/../../../c" 之类穿越路径
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def _sanitize_name(name: str, suffix: str = "") -> str:
    """校验并规范化文件名（不含路径分隔符）。

    Args:
        name: Agent 提供的文件名（不含扩展名）
        suffix: 强制后缀（如 ".py"、".md"）

    Returns:
        规范化后的文件名（带后缀）

    Raises:
        ValueError: name 含非法字符（路径分隔符 / 空字符串 / ..）
    """
    if not name or not _SAFE_NAME.match(name):
        raise ValueError(
            f"非法文件名 '{name}'：只允许字母、数字、下划线、连字符、点号，"
            f"不允许路径分隔符或空字符串"
        )
    if not name.endswith(suffix):
        name = name + suffix
    return name


def make_writer_tools(backend) -> list:
    """构建写工具集（5 写 + 1 edit）。

    Args:
        backend: FilesystemBackend 实例（virtual_mode=True，root_dir=harnesses/current/）
    """
    if backend is None:
        raise ValueError("writers 需要 backend 实例（make_evolve_tools 传入）")

    # ── 5 个专用写工具 ──────────────────────────────────────────

    @tool
    def write_prompt(name: str, content: str) -> str:
        """新建一个提示词文件。

        写入 harness 包的 prompts/ 目录。仅用于新建——文件已存在会报错，
        此时请用 edit_source 修改已有文件。

        Args:
            name: 提示词文件名（不含 .md 后缀，如 "writing_system"）
            content: 提示词全文（markdown）
        """
        return _write_element(backend, "prompts", name, ".md", content, "提示词")

    @tool
    def write_middleware(name: str, code: str) -> str:
        """新建一个中间件源码文件。

        写入 harness 包的 middleware/ 目录。文件必须是符合 DeepAgent
        AgentMiddleware 规范的 Python 代码（含 state_schema / hook / 返回 dict）。
        仅用于新建——文件已存在请用 edit_source。

        Args:
            name: 文件名（不含 .py 后缀，如 "pacing"）
            code: 完整的 Python 源码
        """
        return _write_element(backend, "middleware", name, ".py", code, "中间件")

    @tool
    def write_tool(name: str, code: str) -> str:
        """新建一个工具定义源码文件。

        写入 harness 包的 tools/ 目录。文件必须定义可被 create_deep_agent
        挂载的工具（langchain @tool 装饰器或 BaseTool 子类）。
        仅用于新建——文件已存在请用 edit_source。

        Args:
            name: 文件名（不含 .py 后缀，如 "word_count"）
            code: 完整的 Python 源码
        """
        return _write_element(backend, "tools", name, ".py", code, "工具")

    @tool
    def write_skill(path: str, content: str) -> str:
        """新建一个技能包文件。

        写入 harness 包的 skills/ 目录。技能是 markdown + 可选脚本，
        path 可含子目录（如 "writing/chapter-writing/SKILL.md"）。

        Args:
            path: 相对 skills/ 的路径（如 "meta/auto-pipeline/SKILL.md"）
            content: 技能文件全文
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        # path 允许子目录但不允许 .. 穿越
        if ".." in path or path.startswith("/"):
            return f"错误：非法路径 '{path}'（不允许 .. 或绝对路径）"
        virt_path = f"/skills/{path}"
        result = backend.write(virt_path, content)
        if result.error:
            return f"写入失败（{result.error}）。如果文件已存在，请用 edit_source 修改。"
        ctx.emit_step("write_skill", "done", path=virt_path)
        return f"技能文件已创建：{virt_path}"

    @tool
    def write_subagent(name: str, code: str) -> str:
        """新建一个子代理定义源码文件。

        写入 harness 包的 subagents/ 目录。文件必须定义子代理的构建逻辑
        （被 assemble() 调用构建 CompiledSubAgent）。
        仅用于新建——文件已存在请用 edit_source。

        Args:
            name: 文件名（不含 .py 后缀，如 "editor"）
            code: 完整的 Python 源码
        """
        return _write_element(backend, "subagents", name, ".py", code, "子代理")

    # ── 1 个通用编辑工具 ───────────────────────────────────────

    @tool
    def edit_source(file_path: str, old_string: str, new_string: str) -> str:
        """修改 harness 包内已有文件的某段内容（精确字符串替换）。

        在指定文件中搜索 old_string，替换为 new_string。
        - old_string 必须在文件中唯一出现（否则用更大的上下文缩小匹配范围）。
        - old_string 和 new_string 不能相同。
        - 文件不存在会报错。

        Args:
            file_path: 相对 harness 包根的文件路径（如 "middleware/goal.py"）
            old_string: 要替换的原文（精确匹配，含缩进）
            new_string: 替换后的新文本
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        virt_path = "/" + file_path.lstrip("/")
        if ".." in virt_path:
            return f"错误：非法路径 '{file_path}'（不允许 ..）"
        result = backend.edit(virt_path, old_string, new_string)
        if result.error:
            return f"编辑失败（{file_path}）：{result.error}"
        ctx.emit_step("edit_source", "done", path=virt_path, occurrences=result.occurrences)
        return f"已编辑 {file_path}（替换 {result.occurrences} 处）"

    return [write_prompt, write_middleware, write_tool, write_skill, write_subagent, edit_source]


def _write_element(backend, subdir: str, name: str, suffix: str, content: str, label: str) -> str:
    """专用写工具的共享内核：sanitize name → backend.write → emit_step。"""
    ctx = get_tool_context()
    if ctx is None:
        return "错误：session 未初始化"
    try:
        safe = _sanitize_name(name, suffix)
    except ValueError as e:
        return str(e)
    virt_path = f"/{subdir}/{safe}"
    result = backend.write(virt_path, content)
    if result.error:
        return (
            f"写入失败（{result.error}）。如果文件已存在，请用 edit_source 修改已有文件。"
        )
    ctx.emit_step(f"write_{label}", "done", path=virt_path)
    return f"{label}文件已创建：{virt_path}"


__all__ = ["make_writer_tools"]
