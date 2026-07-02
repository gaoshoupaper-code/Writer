"""surface 读取类工具（决策 V1-V7：按被评估 trace 的版本对齐要素）。

读被评估 Agent 版本的「设计意图 + 结构索引」，用于判断「流程本该如何」。
版本对齐：working 版本读当前工作区，snapshot 版本读该快照的历史要素。
- read_surface()           Agent 地图目录（config + 结构化推导 + 文件清单）
- read_source_file(path)   按版本读单个源码文件全文

版本感知 helper（本模块私有，只被上面两工具用）：
  _resolve_version / _get_config_for_version / _list_source_files
  / _read_source_by_version / _derive_agent_map
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.core import git_ops
from app.core.settings import settings
from app.harness_config.bootstrap import build_v1_config
from app.eval_agent.ctx import get_eval_context
from app.versioning import snapshot_repo

logger = logging.getLogger("evolution.eval_agent.tools.surface")


# ── 版本感知辅助（决策 V1-V7：按被评估 trace 的版本对齐要素）─────────


def _resolve_version() -> tuple[str, str | None]:
    """解析当前评估的版本基准，返回 (版本类型, source_commit 或 "working")。

    - working 版本 → ("working", "working")，读当前 working 区。
    - snapshot 版本 → ("snapshot", <source_commit>)，读 git 历史版本。
    - 无版本记录（trace 非 manual_tests 产生）→ 降级为 working。

    版本基准以 EvaluationContext.agent_version_type/id 为准（V3）。
    """
    ctx = get_eval_context()
    if ctx is None or ctx.agent_version_type != "snapshot" or ctx.agent_version_id is None:
        return ("working", "working")
    # snapshot：从 DB 取该版本的 source_commit
    snap = snapshot_repo.get_snapshot(ctx.agent_version_id)
    if snap and snap.get("source_commit"):
        return ("snapshot", snap["source_commit"])
    # snapshot 记录不存在或无 source_commit，降级 working
    logger.warning(
        "eval %s: snapshot v%s 无 source_commit，降级 working",
        ctx.eval_id if ctx else "?", ctx.agent_version_id,
    )
    return ("working", "working")


def _get_config_for_version(version_type: str) -> dict[str, Any]:
    """按版本取 config（V1/V2）。

    working → build_v1_config()（当前 working 区）。
    snapshot → get_snapshot_config(version_id)（DB 存的 config_json）。
    """
    ctx = get_eval_context()
    if version_type == "snapshot" and ctx and ctx.agent_version_id is not None:
        cfg = snapshot_repo.get_snapshot_config(ctx.agent_version_id)
        if cfg:
            return cfg
    return build_v1_config()


def _list_source_files(version_type: str) -> list[str]:
    """按版本列 harness 包源码文件清单（相对路径）。

    working → rglob 当前 working 区。
    snapshot → git ls-tree -r --name-only <commit>（列该 commit 的文件）。
    """
    pkg_dir = settings.harness_work_dir_path
    if version_type == "snapshot":
        ctx = get_eval_context()
        if ctx and ctx.agent_version_id is not None:
            snap = snapshot_repo.get_snapshot(ctx.agent_version_id)
            if snap and snap.get("source_commit"):
                try:
                    out = git_ops._git(  # noqa: SLF001
                        ["ls-tree", "-r", "--name-only", snap["source_commit"]],
                        git_ops.work_dir(),
                    )
                    return [
                        f for f in out.splitlines()
                        if f and ".git" not in f and "__pycache__" not in f
                    ]
                except Exception:
                    logger.warning("git ls-tree 失败，降级列 working 区文件")
    # working 或降级
    files = []
    for p in sorted(pkg_dir.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            rel = p.relative_to(pkg_dir)
            files.append(str(rel).replace("\\", "/"))
    return files


def _read_source_by_version(file_path: str, version_type: str, source_commit: str) -> str:
    """按版本读单个源码文件全文（V1/V4：read_source_file 的内核）。

    working → 读 harnesses/current/<path>。
    snapshot → git show <commit>:<path>。
    """
    if version_type == "snapshot" and source_commit != "working":
        return git_ops.show_file(source_commit, file_path)
    # working
    full = settings.harness_work_dir_path / file_path
    return full.read_text(encoding="utf-8")


def _derive_agent_map(config: dict[str, Any], files: list[str]) -> str:
    """从 config 推导 Agent 地图目录（V5/V6）。

    结构化展示：
      - 各 agent 的 processors 装配（middleware 类名 + 参数）
      - 各 agent 的 slots（prompt slot 名）
      - 文件按 config 引用归类（middleware/*.py → processor 类，prompts/*.md → slot）
      - 工具定义推导（从 config 的 processors/slots 推导）
    不内联源码全文（用 read_source_file 按需读）。
    """
    lines: list[str] = []

    def _describe_agent(name: str, agent_cfg: dict[str, Any]) -> None:
        lines.append(f"### Agent: {name}")
        slots = agent_cfg.get("slots", {})
        if slots:
            lines.append(f"- slots（prompt 位）: {list(slots.keys())}")
        processors = agent_cfg.get("processors", [])
        if processors:
            lines.append(f"- processors（middleware 装配）:")
            for p in processors:
                cls = p.get("class", "?")
                hook = p.get("hook", "")
                params = p.get("params", {})
                param_keys = list(params.keys()) if isinstance(params, dict) else []
                lines.append(f"    · [{hook}] {cls}（参数: {param_keys}）")

    # 主 pipeline
    meta = config.get("meta_pipeline") or config.get("meta") or {}
    if meta:
        lines.append("## 主 Agent（meta_pipeline）")
        _describe_agent("meta_pipeline", meta)
        lines.append("")

    # 子代理
    subagents = config.get("subagents") or []
    if isinstance(subagents, list):
        lines.append(f"## 子代理（{len(subagents)} 个）")
        for sa in subagents:
            name = sa.get("name", "?")
            _describe_agent(name, sa)
            lines.append("")

    # 文件归类（按目录前缀）
    lines.append("## harness 包文件清单（按目录归类）")
    by_dir: dict[str, list[str]] = {}
    for f in files:
        top = f.split("/")[0] if "/" in f else "(根)"
        by_dir.setdefault(top, []).append(f)
    for d in sorted(by_dir):
        lines.append(f"- {d}/（{len(by_dir[d])} 个）")
        for f in by_dir[d][:8]:  # 每目录最多列 8 个
            lines.append(f"    {f}")
        if len(by_dir[d]) > 8:
            lines.append(f"    …还有 {len(by_dir[d]) - 8} 个")

    lines.append("")
    lines.append(
        "（如需查看某文件全文，调用 read_source_file(path)——"
        "它会按被评估的 Agent 版本读取，与 read_surface 版本一致）"
    )
    return "\n".join(lines)


# ── surface 类工具 ─────────────────────────────────────────────


def make_surface_tools() -> list:
    """构建 surface 读取类工具。"""

    @tool
    def read_surface() -> str:
        """读取被评估 Agent 版本的「Agent 地图目录」（设计意图 + 结构索引）。

        返回内容（按被评估 trace 对应的 Agent 版本，V1-V7）：
          - HarnessConfig 全文（agent 装配 / middleware / processors / 参数）
          - 从 config 推导的结构化地图（各 agent 的 processors 装配 + slots + 文件归类）
          - 工具定义推导清单

        用于判断「流程本该如何」（设计意图），对照实际 trace 找偏差。
        **不内联源码全文**——需要看某文件全文时用 read_source_file(path)（按版本读，版本一致）。

        版本对齐：working 版本读当前 working 区，snapshot 版本读该快照的历史要素。
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        try:
            version_type, source_commit = _resolve_version()
            config = _get_config_for_version(version_type)
            config_str = json.dumps(config, ensure_ascii=False, indent=2)
            files = _list_source_files(version_type)
            agent_map = _derive_agent_map(config, files)

            version_desc = (
                f"working 版本（读当前工作区）" if version_type == "working"
                else f"snapshot 版本 v{ctx.agent_version_id}（commit={source_commit}，读历史版本）"
            )
            return (
                f"## Agent 版本\n{version_desc}\n\n"
                f"## HarnessConfig\n```json\n{config_str}\n```\n\n"
                f"## Agent 地图目录\n{agent_map}"
            )
        except Exception as e:
            return f"读 surface 失败：{e}"

    @tool
    def read_source_file(file_path: str) -> str:
        """【按版本读取】读取被评估 Agent 版本的某个源码文件全文。

        版本与 read_surface 一致（V1/V4）：
          - working 版本 → 读当前 harnesses/current/<file_path>
          - snapshot 版本 → git show <commit>:<file_path>（读历史版本）

        这是评估 Agent 读源码全文的**唯一**途径（read_file 已禁用）。
        file_path 是相对 harness 包根的路径（如 "middleware/pacing.py"、"prompts/writing.md"），
        可从 read_surface 的文件清单获取。

        Args:
            file_path: 相对 harness 包根的源码文件路径
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        ctx.emit_step("read_source_file", "running", file_path=file_path)
        try:
            version_type, source_commit = _resolve_version()
            content = _read_source_by_version(file_path, version_type, source_commit)
            ctx.emit_step("read_source_file", "done", file_path=file_path, chars=len(content))
            return content
        except Exception as e:
            ctx.emit_step("read_source_file", "failed", file_path=file_path, error=str(e))
            return f"读源码失败（{file_path}）：{e}"

    return [read_surface, read_source_file]


__all__ = ["make_surface_tools"]
