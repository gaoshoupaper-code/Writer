"""elements_api —— Agent 要素展示端点（去 DB 重构：数据源从 config → git 源文件）。

从 harness 独立仓库的 git commit 读取真实源文件，投影成面向展示的结构化视图，
供前端「Agent 要素」页渲染（Prompt/Skills/Middleware/Subagents 四要素）。

数据源变更（去 DB 重构）：
  旧：从 DB harness_snapshots.config_json 提取 agent 结构 + git show 读全文
  新：完全从 git 仓库的目录结构推导 agent 结构 + git show 读全文
  含义：展示的是真实运行的 agent（源文件），而非死代码 config 的投影。

端点（/api/snapshots 前缀）：
  GET /snapshots/{version}/elements   版本要素展示视图
  GET /snapshots/{version}/source     指定文件源码（middleware 懒加载用）
"""
from __future__ import annotations

import ast
import logging
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query

from app.core.git_ops import show_file, log_oneline
from app.versioning import registry_repo

logger = logging.getLogger("evolution.elements_api")

router = APIRouter(prefix="/snapshots", tags=["snapshots"])

# subagent 机器名 → 中文角色名。
# harness 包里 subagents/ 的 build_* 一一对应（与 assemble 装配顺序一致）。
_SUBAGENT_ORDER = ["interview", "storybuilding", "detail_outline", "writing"]
_SUBAGENT_ROLE_MAP: dict[str, str] = {
    "interview": "需求访谈",
    "storybuilding": "故事构建",
    "detail_outline": "细纲生成",
    "writing": "正文写作",
    "general_purpose": "通用助手",
}


# ── git 源文件读取辅助 ─────────────────────────────────────────


def _version_to_commit(version: int) -> str | None:
    """version 编号 → git commit hash（通过 git log 顺序映射）。"""
    v = registry_repo.get_version(version)
    if v is None:
        return None
    log = log_oneline()
    commits = [line.split()[0] for line in log if line.strip()]
    if 1 <= version <= len(commits):
        return commits[len(commits) - version]
    return None


def _list_files_at_commit(commit: str, subdir: str) -> list[str]:
    """列某 commit 下指定子目录的所有文件路径（相对仓库根）。

    Args:
        commit: git commit hash
        subdir: 子目录（如 "prompts"、"skills"、"middleware"）
    """
    try:
        from app.core.git_ops import _git, work_dir
        out = _git(["ls-tree", "-r", "--name-only", commit, subdir], work_dir())
        return [f for f in out.splitlines() if f.strip()] if out.strip() else []
    except Exception:  # noqa: BLE001
        logger.debug("ls-tree 失败: %s @ %s", subdir, commit, exc_info=True)
        return []


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """从 markdown 全文提 YAML front matter（首尾 --- 之间）。无则返回 {}。"""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


def _read_prompt(commit: str | None, name: str) -> str:
    """读 prompts/{name}.md 全文。commit=None 或读取失败返回空串。"""
    if not commit:
        return ""
    try:
        return show_file(commit, f"prompts/{name}")
    except Exception:  # noqa: BLE001
        logger.debug("prompt 读取失败: %s @ %s", name, commit)
        return ""


def _build_skill_infos(commit: str | None) -> list[dict[str, Any]]:
    """扫 skills/ 目录，读每个 SKILL.md 的全文 + frontmatter description。"""
    if not commit:
        return []
    skill_files = _list_files_at_commit(commit, "skills")
    skills: list[dict[str, Any]] = []
    seen_dirs: set[str] = set()
    for f in skill_files:
        if not f.endswith("/SKILL.md"):
            continue
        # skill 路径 = SKILL.md 的父目录（如 skills/meta/auto-pipeline）
        skill_path = f.rsplit("/SKILL.md", 1)[0]
        if skill_path in seen_dirs:
            continue
        seen_dirs.add(skill_path)
        name = skill_path.split("/")[-1]
        try:
            content = show_file(commit, f)
            description = _parse_frontmatter(content).get("description")
            skills.append({"path": skill_path, "name": name,
                           "description": description, "content": content, "load_error": None})
        except Exception as e:  # noqa: BLE001
            skills.append({"path": skill_path, "name": name,
                           "description": None, "content": None, "load_error": str(e)})
    return skills


def _build_middleware_infos(commit: str | None) -> list[dict[str, Any]]:
    """扫 middleware/ 目录，读每个 .py 的类名 + 模块 docstring（用途说明）。"""
    if not commit:
        return []
    py_files = [f for f in _list_files_at_commit(commit, "middleware") if f.endswith(".py")]
    middlewares: list[dict[str, Any]] = []
    for f in py_files:
        class_name = f.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # 文件名（snake_case）
        description: str | None = None
        try:
            src = show_file(commit, f)
            description = ast.get_docstring(ast.parse(src))
        except Exception:  # noqa: BLE001
            logger.debug("middleware docstring 解析失败: %s @ %s", f, commit)
        middlewares.append({
            "class_name": class_name,
            "source_path": f,
            "description": description,
        })
    return middlewares


# ── 视图构建 ────────────────────────────────────────────────────


def build_elements_view(version: int) -> dict[str, Any]:
    """从 git 仓库构建版本要素展示视图。

    结构（对齐前端 ElementsView 类型）：
      {
        "version": int,
        "source_commit": str | None,
        "has_source": bool,
        "agents": [ {name, kind, prompt, skills, middlewares}, ... ],
        "subagent_relations": [ {from, to, role}, ... ]
      }

    meta agent 始终排第一；subagents 按固定装配顺序。
    """
    commit = _version_to_commit(version)
    skills = _build_skill_infos(commit)
    middlewares = _build_middleware_infos(commit)

    agents: list[dict[str, Any]] = []

    # meta agent：prompt 从 prompts/meta_system.md 读
    agents.append({
        "name": "meta",
        "kind": "meta",
        "prompt": {"body": _read_prompt(commit, "meta_system.md")},
        "skills": [s for s in skills if s["path"].startswith("skills/meta/")],
        "middlewares": middlewares,
    })

    # subagents：按固定装配顺序，各自读 prompt
    for sub_name in _SUBAGENT_ORDER:
        # subagent 的 prompt 文件名规律：{name}_system.md
        prompt_file = f"{sub_name}_system.md"
        sub_skills = [s for s in skills if s["path"].startswith(f"skills/{sub_name}")]
        agents.append({
            "name": sub_name,
            "kind": "subagent",
            "prompt": {"body": _read_prompt(commit, prompt_file)},
            "skills": sub_skills,
            "middlewares": [],  # subagent 的 middleware 是通用三件套，不单独展示
        })

    relations = [
        {"from": "meta", "to": s, "role": _SUBAGENT_ROLE_MAP.get(s, s)}
        for s in _SUBAGENT_ORDER
    ]

    return {
        "version": version,
        "source_commit": commit,
        "has_source": commit is not None,
        "agents": agents,
        "subagent_relations": relations,
    }


# ── 端点 ────────────────────────────────────────────────────────


@router.get("/{version}/elements")
def get_elements(version: int) -> dict[str, Any]:
    """版本要素展示视图（从 git 源文件读取）。version 不存在则 404。"""
    v = registry_repo.get_version(version)
    if v is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
    return build_elements_view(version)


@router.get("/{version}/source")
def get_source(
    version: int,
    path: str = Query(..., description="相对 harness 包根的文件路径，如 middleware/goal.py"),
) -> dict[str, Any]:
    """读指定版本指定文件的源码全文（middleware 懒加载用）。"""
    commit = _version_to_commit(version)
    if not commit:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 无对应 commit（可能为迁移历史版本）")

    try:
        content = show_file(commit, path)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "does not exist" in msg or "exists on disk, but not in" in msg:
            raise HTTPException(status_code=404, detail=f"{path} 在 v{version} 不存在")
        logger.warning("源码读取失败: %s @ v%s → %s", path, version, msg)
        raise HTTPException(status_code=500, detail=f"源码读取失败: {msg}")

    return {"path": path, "content": content}
