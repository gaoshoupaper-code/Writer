"""Skills 自进化系统的加载器（DD7b）。

按 owner 选中的 skill_id，返回 SKILL.md 目录的绝对路径列表。
复用现有 _compose_skills_backend 把这些路径注入 agent 的 backend。

物理结构（DD7b）：executor/skills/<owner_id>/<skill_id>/SKILL.md
作用域：严格个人（D2），A 用户的长出来的 Skill B 用户看不到。
"""

from __future__ import annotations

from pathlib import Path

from app.platform.core.db import SkillRepository, get_database


def skills_root() -> Path:
    """Skills 自进化系统根目录（executor/skills/，与 workspace 平级）。"""
    return Path(__file__).resolve().parents[3] / "skills"


def resolve_owner_skills(
    owner_id: str, selected_skill_ids: list[str] | None,
) -> list[str]:
    """返回 owner 选中的 Skill 目录绝对路径列表（DD7b/7c）。

    Args:
        owner_id: 用户 ID
        selected_skill_ids: D9 Agent 推荐 + 用户确认要加载的 skill_id 列表。
                            None/空 = 不加载（纯冷启动 D20）。

    Returns:
        目录路径列表（只含 DB 有记录且 SKILL.md 文件存在的，双写一致性兜底）。
    """
    if not selected_skill_ids:
        return []
    repo = SkillRepository(get_database())
    base = skills_root() / owner_id
    result: list[str] = []
    for sid in selected_skill_ids:
        meta = repo.get(sid, owner_id)
        skill_md = base / sid / "SKILL.md"
        if meta and skill_md.exists():
            result.append(str(base / sid))
    return result


__all__ = ["skills_root", "resolve_owner_skills"]
