"""包级 proposer（Phase 7，取代 proposer.save_surface_candidate 的存储职责）。

复用 proposer.generate_candidate_surface 的 LLM 生成能力，但输出目标从
surface_versions DB 行改为包内文件（整包级，D6=①）。

流程差异（旧 surface 级 → 新整包级）：
  旧：proposer 生成单 surface content → save_surface_candidate 写 DB 行
  新：proposer 生成单文件 content → 写回包内文件 → publish_and_notify 发快照

为什么保留"单文件"粒度的生成（而非整包一次性生成）：
  LLM 一次改一个文件（bounded change），符合现有 proposer 的设计（最小改动）。
  整包是多个文件的集合，但每次 propose 仍针对单个文件（如改 writing_system.md）。
  文件定位 = 旧 surface 定位的等价物（surface_type→文件类型，surface_name→文件名）。

文件定位映射（surface 三元组 → 包内文件路径）：
  prompt + writing_system + writing  → prompts/writing_system.md
  prompt + storybuilding_evaluation + storybuilding → prompts/storybuilding_evaluation.md
  stateful_middleware + GoalMiddleware + meta → middleware/goal.py
  skill + chapter-writing + writing → skills/writing/chapter-writing/SKILL.md
  ...（按文件类型 + 名字 + scope 映射到包内路径）

设计依据：设计文档 D6=①（整包版本，但 propose 仍单文件改）+ T5.1。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.settings import settings

logger = logging.getLogger("evolution.package_proposer")


def _package_dir() -> Path:
    """Agent 包目录（evolution/harnesses/current/）。"""
    import app.core.settings as _settings_mod
    evolution_root = Path(_settings_mod.__file__).resolve().parents[2]
    return evolution_root / "harnesses" / "current"


# ── surface 三元组 → 包内文件路径映射 ────────────────────────


def resolve_package_file(surface_type: str, surface_name: str, scope: str) -> Path | None:
    """surface 三元组映射到包内文件路径。无法映射返回 None。

    映射规则（与 migrate_to_surface 的反向映射对齐）：
      prompt/<name> → prompts/<name>.md
      skill/<name>  → skills/<scope 路径>/<name>/SKILL.md（scope 决定子目录）
      description/<name> → 无独立文件（描述内联在 assemble，暂不进化）
      stateful_middleware/<name> → middleware/<文件名>.py（按名字映射）
      middleware_params/<name> → 无独立文件（参数内联在 assemble）
    """
    pkg = _package_dir()
    if surface_type == "prompt":
        return pkg / "prompts" / f"{surface_name}.md"
    if surface_type == "skill":
        return _resolve_skill_path(pkg, surface_name, scope)
    if surface_type == "stateful_middleware":
        return _resolve_middleware_path(pkg, surface_name)
    return None


def _resolve_skill_path(pkg: Path, name: str, scope: str) -> Path | None:
    """skill 名 + scope → 包内 SKILL.md 路径。"""
    skill_map = {
        ("storybuilding-initial", "storybuilding"): "skills/storybuilding-initial/SKILL.md",
        ("storybuilding-expand", "storybuilding"): "skills/storybuilding-expand/SKILL.md",
        ("detail-planning", "detail-outline"): "skills/detail_outline/detail-planning/SKILL.md",
        ("chapter-writing", "writing"): "skills/writing/chapter-writing/SKILL.md",
        ("auto-pipeline", "meta"): "skills/meta/auto-pipeline/SKILL.md",
        ("interactive-gating", "meta"): "skills/meta/interactive-gating/SKILL.md",
    }
    rel = skill_map.get((name, scope))
    return pkg / rel if rel else None


def _resolve_middleware_path(pkg: Path, name: str) -> Path | None:
    """middleware 类名 → 包内 .py 文件名。"""
    mw_map = {
        "GoalMiddleware": "middleware/goal.py",
        "ErrorRecoveryMiddleware": "middleware/error_recovery.py",
        "FilesystemPathGuardMiddleware": "middleware/path_guard.py",
        "FileWriteSerializeMiddleware": "middleware/file_write_serialize.py",
        "ArtifactPrerequisiteMiddleware": "middleware/artifact_prerequisite.py",
        "MetaReadOnlyMiddleware": "middleware/meta_readonly.py",
        "RevisionLimitMiddleware": "middleware/revision_limit.py",
        "StorylineSingleLineLimitMiddleware": "middleware/storyline_single_line_limit.py",
    }
    rel = mw_map.get(name)
    return pkg / rel if rel else None


# ── 包文件读写（proposer 的存储层替代）────────────────────────


def read_current_file(surface_type: str, surface_name: str, scope: str) -> str | None:
    """读包内当前文件内容（proposer 输入）。文件不存在返回 None。"""
    path = resolve_package_file(surface_type, surface_name, scope)
    if path is None or not path.exists():
        logger.warning("无法定位包文件: %s/%s/%s", surface_type, surface_name, scope)
        return None
    return path.read_text(encoding="utf-8")


def write_candidate_file(
    surface_type: str,
    surface_name: str,
    scope: str,
    content: str,
) -> Path | None:
    """把 proposer 生成的候选 content 写回包内文件。

    直接覆盖包内文件（current 包是可编辑真理源，D7=c'）。
    写后由调用方决定是否 publish_and_notify 发快照。

    Returns: 写入的文件路径，或 None（定位失败）。
    """
    path = resolve_package_file(surface_type, surface_name, scope)
    if path is None:
        logger.error("无法定位包文件，写入失败: %s/%s/%s", surface_type, surface_name, scope)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info("候选已写入包文件: %s", path)
    return path


# ── 包级 propose（复用 proposer.generate_candidate_surface）────────


def propose_package_file(
    signature_id: int,
    surface_type: str,
    surface_name: str,
    scope: str,
    *,
    validate_fn=None,
) -> dict[str, Any] | None:
    """针对包内单个文件跑 propose 流程（生成 + 校验 + 写回）。

    与 proposer.propose_surface_with_retry 的区别：
    - 输入来源：read_current_file（包内文件）而非 surface_repo.get_approved_version
    - 输出目标：write_candidate_file（写包文件）而非 save_surface_candidate（写 DB 行）
    - LLM 生成逻辑：复用 proposer.generate_candidate_surface（不变）

    Args:
        signature_id: 失败签名 ID
        surface_type/surface_name/scope: 文件定位（surface 三元组）
        validate_fn: 校验函数（默认 static_check）

    Returns: {file_path, attempts, signature_id, final_error} 或 None。
    """
    from app.improvement import proposer, static_check

    current_content = read_current_file(surface_type, surface_name, scope)
    if current_content is None:
        return None

    # 复用 proposer 的带重试生成（LLM 调用 + 校验逻辑不变）
    result = proposer.propose_surface_with_retry(
        signature_id, surface_type, surface_name, scope,
        current_content, validate_fn=validate_fn,
    )
    if result is None or result.get("content") is None:
        return result  # 全部失败

    # 写回包文件（替代 save_surface_candidate）
    written = write_candidate_file(
        surface_type, surface_name, scope, result["content"],
    )
    if written is None:
        return {**result, "final_error": "包文件写入失败"}

    return {
        "file_path": str(written),
        "attempts": result["attempts"],
        "signature_id": signature_id,
        "surface_type": surface_type,
        "surface_name": surface_name,
        "scope": scope,
        "final_error": None,
    }
