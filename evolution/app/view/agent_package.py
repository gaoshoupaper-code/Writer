"""Agent 包透视 API（D8）。

GET /api/agent-package：扫描 evolution/harnesses/current/ 包目录，返回六段结构：
  manifest / prompts / skills / middleware / subagents / assemble。

prompts 双层语义：包内 prompts/*.md = 当前部署快照；prompts 表 = 版本历史。
两者都返回，前端展示"当前正文 + 历史版本列表"。

设计依据：设计文档 D8 + 需求决策 9-14。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

import app.core.db as db

logger = logging.getLogger("evolution.agent_package")

router = APIRouter(tags=["agent-package"])

# 包目录：项目根/evolution/harnesses/current
_PACKAGE_DIR = Path(__file__).resolve().parent.parent.parent / "harnesses" / "current"

# middleware 文件名 → 类名映射（去 .py）。用于 schema_lock 的 C 类判定。
_C_CLASS_BASENAMES = {"goal"}


@router.get("/agent-package")
def get_agent_package() -> dict[str, Any]:
    """返回 Agent 包六段透视结构。"""
    if not _PACKAGE_DIR.is_dir():
        return {"error": "package not found", "path": str(_PACKAGE_DIR)}

    return {
        "manifest": _read_manifest(),
        "prompts": _read_prompts(),
        "skills": _read_skills(),
        "middleware": _read_middleware(),
        "subagents": _read_subagents(),
        "assemble": _read_assemble(),
    }


def _read_manifest() -> dict[str, Any]:
    """读 manifest.json。"""
    path = _PACKAGE_DIR / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("manifest.json 解析失败", exc_info=True)
        return {}


def _read_prompts() -> list[dict[str, Any]]:
    """prompts 段：包内 md 当前正文 + prompts 表版本历史。

    包内 prompts/<name>.md 是当前部署快照（实际运行的）。
    prompts 表按 name 匹配，取版本历史（version/labels/commit_message/source/created_at）。
    """
    prompts_dir = _PACKAGE_DIR / "prompts"
    result: list[dict[str, Any]] = []

    # 包内 md 文件名（去 .md）= prompt name
    md_files = sorted(prompts_dir.glob("*.md")) if prompts_dir.is_dir() else []

    # 批量查 prompts 表版本历史（一次查全部，避免 N 次 IO）
    versions_map = _load_prompt_versions_map()

    for md in md_files:
        name = md.stem
        try:
            current_md = md.read_text(encoding="utf-8").strip()
        except Exception:
            current_md = ""
        result.append({
            "name": name,
            "current_md": current_md,
            "versions": versions_map.get(name, []),
        })
    return result


def _read_skills() -> list[dict[str, Any]]:
    """skills 段：扫 skills/**/SKILL.md，按目录推断 scope。"""
    skills_dir = _PACKAGE_DIR / "skills"
    result: list[dict[str, Any]] = []
    if not skills_dir.is_dir():
        return result

    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        # scope = SKILL.md 相对 skills/ 的第一级目录（detail_outline → detail-outline）
        rel = skill_md.relative_to(skills_dir)
        scope_raw = rel.parts[0] if rel.parts else "unknown"
        # 包内目录用下划线（detail_outline），scope 语义用连字符（detail-outline）
        scope = scope_raw.replace("_", "-")
        # name = SKILL.md 的父目录名（如 chapter-writing / auto-pipeline）
        name = skill_md.parent.name
        try:
            content = skill_md.read_text(encoding="utf-8").strip()
        except Exception:
            content = ""
        result.append({"scope": scope, "name": name, "content": content})
    return result


def _read_middleware() -> list[dict[str, Any]]:
    """middleware 段：扫 middleware/*.py，标 C 类。

    C 类判定：manifest.schema_lock.c_surfaces 里的 name（去 Middleware 后缀的小写基名）。
    GoalMiddleware → goal → 在 _C_CLASS_BASENAMES 或 schema_lock 里则为 C 类。
    """
    mw_dir = _PACKAGE_DIR / "middleware"
    result: list[dict[str, Any]] = []
    if not mw_dir.is_dir():
        return result

    # schema_lock 里的 C 类基名集合
    c_names = _load_c_surface_names()

    for py in sorted(mw_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        basename = py.stem  # goal / error_recovery / ...
        is_c = basename in c_names or basename in _C_CLASS_BASENAMES
        result.append({"filename": py.name, "is_c_class": is_c})
    return result


def _read_subagents() -> list[dict[str, Any]]:
    """subagents 段：扫 subagents/*.py（含 evaluators 子目录），标 role。"""
    sub_dir = _PACKAGE_DIR / "subagents"
    result: list[dict[str, Any]] = []
    if not sub_dir.is_dir():
        return result

    for py in sorted(sub_dir.rglob("*.py")):
        if py.name.startswith("_"):
            continue
        rel = py.relative_to(sub_dir)
        # role：顶层文件名（interview/storybuilding/...）；evaluators/ 下标 evaluation
        role = rel.stem if len(rel.parts) == 1 else f"evaluator:{rel.stem}"
        result.append({"filename": str(rel).replace("\\", "/"), "role": role})
    return result


def _read_assemble() -> dict[str, Any]:
    """assemble 段：装配拓扑（meta middleware 列表 + subagent 列表）。

    从 __init__.py 的 assemble 逻辑提取——这里用手写映射（assemble 结构稳定，
    变动时同步更新）。meta_middleware 是 meta 层实例化的顺序；subagents 是 5 个。
    """
    return {
        "meta_middleware": [
            "ErrorRecoveryMiddleware",
            "MetaReadOnlyMiddleware",
            "FilesystemPathGuardMiddleware",
            "FileWriteSerializeMiddleware",
            "GoalMiddleware",
        ],
        "subagents": [
            "general-purpose",
            "interview",
            "storybuilding",
            "detail_outline",
            "writing",
        ],
    }


# ── 辅助：prompts 表版本历史查询 ──


def _load_prompt_versions_map() -> dict[str, list[dict[str, Any]]]:
    """批量查 prompts 表，返回 {name: [version_summary, ...]}。

    只取版本元数据（不取 content，content 太大，前端按需拉）。
    """
    try:
        rows = db.query_all(
            """SELECT p.name AS prompt_name, pv.version, pv.labels, pv.commit_message,
                      pv.source, pv.created_at
               FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id
               ORDER BY p.name, pv.version DESC"""
        )
    except Exception:
        logger.debug("prompts 版本查询失败（表可能为空）", exc_info=True)
        return {}

    result: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        name = r["prompt_name"]
        labels = [s.strip() for s in (r["labels"] or "").split(",") if s.strip()]
        result.setdefault(name, []).append({
            "version": r["version"],
            "labels": labels,
            "commit_message": r["commit_message"],
            "source": r["source"],
            "created_at": r["created_at"],
        })
    return result


def _load_c_surface_names() -> set[str]:
    """从 manifest.schema_lock 提取 C 类 surface 的基名集合。

    manifest.json 的 schema_lock.c_surfaces[].name 形如 "GoalMiddleware"。
    提取后转小写基名（goalmiddleware → 去掉 middleware 后缀 → goal）。
    """
    manifest = _read_manifest()
    c_surfaces = manifest.get("schema_lock", {}).get("c_surfaces", [])
    names: set[str] = set()
    for cs in c_surfaces:
        raw = cs.get("name", "")
        # GoalMiddleware → goal
        base = raw.lower()
        if base.endswith("middleware"):
            base = base[: -len("middleware")]
        names.add(base)
    return names
