"""prompt 版本管理数据访问层（Phase 4 T9，langfuse 式）。

职责：
  - prompt CRUD（name 唯一，一条"prompt 线"）
  - 版本管理：version 单调递增（max+1）
  - label 管理：production/latest/staging，同 prompt_id 下一个 label 只指向一个 version（互斥）
  - 按 label/version 拉取（后端 loader 用）

设计依据：设计文档第四轮 T9/T10，langfuse 学习笔记第12章。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import app.db as db

PRODUCTION_LABEL = "production"
LATEST_LABEL = "latest"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_labels(labels_str: str) -> list[str]:
    """labels 存为逗号分隔字符串，解析为列表。"""
    return [s.strip() for s in labels_str.split(",") if s.strip()] if labels_str else []


def _join_labels(labels: list[str]) -> str:
    return ",".join(labels)


# ── prompt 线 CRUD ──────────────────────────────────────────


def create_prompt(name: str, prompt_type: str = "text") -> dict[str, Any]:
    """创建 prompt 线（name 唯一）。已存在则抛 ValueError。"""
    existing = db.query_one("SELECT id FROM prompts WHERE name = ?", (name,))
    if existing:
        raise ValueError(f"Prompt already exists: {name}")
    db.execute(
        "INSERT INTO prompts (name, type, created_at) VALUES (?, ?, ?)",
        (name, prompt_type, _now()),
    )
    return get_prompt_by_name(name)  # type: ignore[return-value]


def get_prompt_by_name(name: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM prompts WHERE name = ?", (name,))


def get_prompt_by_id(prompt_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM prompts WHERE id = ?", (prompt_id,))


def list_prompts() -> list[dict[str, Any]]:
    return db.query_all("SELECT * FROM prompts ORDER BY name")


def delete_prompt(prompt_id: int) -> None:
    """删 prompt 线（级联删所有版本，ON DELETE CASCADE）。"""
    db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))


# ── 版本管理 ────────────────────────────────────────────────


def create_version(
    prompt_id: int,
    content: str,
    commit_message: str | None = None,
    source: str = "manual",
    config: dict[str, Any] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """创建新版本：version = max(version)+1，新版本自动打 latest label。

    label 互斥：若传入 labels，先从同 prompt 其它版本移除这些 label。
    """
    prompt = get_prompt_by_id(prompt_id)
    if prompt is None:
        raise ValueError(f"Prompt not found: {prompt_id}")

    # version 单调递增
    latest_ver = db.query_one(
        "SELECT MAX(version) AS mv FROM prompt_versions WHERE prompt_id = ?", (prompt_id,)
    )
    next_version = (latest_ver["mv"] or 0) + 1 if latest_ver and latest_ver["mv"] else 1

    # labels：新版本默认打 latest；若调用方指定则用调用方的
    final_labels = list(labels) if labels is not None else [LATEST_LABEL]

    # label 互斥：从同 prompt 其它版本移除这些 label
    for label in final_labels:
        _strip_label_from_others(prompt_id, None, label)

    db.execute(
        """INSERT INTO prompt_versions
           (prompt_id, version, content, config, labels, commit_message, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            prompt_id, next_version, content,
            json.dumps(config or {}, ensure_ascii=False),
            _join_labels(final_labels),
            commit_message, source, _now(),
        ),
    )
    return get_version(prompt_id, next_version)  # type: ignore[return-value]


def get_version(prompt_id: int, version: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM prompt_versions WHERE prompt_id = ? AND version = ?",
        (prompt_id, version),
    )


def get_version_by_label(prompt_id: int, label: str) -> dict[str, Any] | None:
    """按 label 拉取版本（label 互斥保证唯一）。

    后端 loader 用此方法读 production label。
    """
    rows = db.query_all(
        "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version DESC",
        (prompt_id,),
    )
    for row in rows:
        if label in _parse_labels(row["labels"] or ""):
            return row
    return None


def list_versions(prompt_id: int) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version DESC",
        (prompt_id,),
    )


def set_labels(version_id: int, labels: list[str]) -> None:
    """设置版本的 labels。label 互斥：同 prompt 其它版本移除这些 label。"""
    ver = db.query_one("SELECT prompt_id FROM prompt_versions WHERE id = ?", (version_id,))
    if ver is None:
        raise ValueError(f"Version not found: {version_id}")
    prompt_id = ver["prompt_id"]
    for label in labels:
        _strip_label_from_others(prompt_id, version_id, label)
    db.execute(
        "UPDATE prompt_versions SET labels = ? WHERE id = ?",
        (_join_labels(labels), version_id),
    )


def _strip_label_from_others(prompt_id: int, keep_version_id: int | None, label: str) -> None:
    """从同 prompt 其它版本移除指定 label（label 互斥）。"""
    rows = db.query_all(
        "SELECT id, labels FROM prompt_versions WHERE prompt_id = ?",
        (prompt_id,),
    )
    for row in rows:
        if keep_version_id is not None and row["id"] == keep_version_id:
            continue
        current = _parse_labels(row["labels"] or "")
        if label in current:
            current.remove(label)
            db.execute(
                "UPDATE prompt_versions SET labels = ? WHERE id = ?",
                (_join_labels(current), row["id"]),
            )


def get_prompt_content(name: str, label: str = PRODUCTION_LABEL) -> dict[str, Any] | None:
    """按 name + label 拉取 prompt 内容（后端 loader 主入口）。

    Returns: {name, version, content, config, labels} 或 None。
    """
    prompt = get_prompt_by_name(name)
    if prompt is None:
        return None
    ver = get_version_by_label(prompt["id"], label)
    if ver is None:
        # label 未找到，回退到最新版本（max version）
        versions = list_versions(prompt["id"])
        if not versions:
            return None
        ver = versions[0]
    return {
        "name": prompt["name"],
        "type": prompt["type"],
        "version": ver["version"],
        "content": ver["content"],
        "config": json.loads(ver["config"] or "{}"),
        "labels": _parse_labels(ver["labels"] or ""),
    }


def get_prompt_version_content(name: str, version: int) -> dict[str, Any] | None:
    """按 name + version 拉取 prompt 内容（trace 详情页用）。

    与 get_prompt_content 不同，这里按具体 version 号取（一条旧 trace 记录的
    可能是已非 production 的历史版本）。找不到返回 None。

    Returns: {name, version, content, config, labels, commit_message, created_at} 或 None。
    """
    prompt = get_prompt_by_name(name)
    if prompt is None:
        return None
    ver = get_version(prompt["id"], version)
    if ver is None:
        return None
    return {
        "name": prompt["name"],
        "type": prompt["type"],
        "version": ver["version"],
        "content": ver["content"],
        "config": json.loads(ver["config"] or "{}"),
        "labels": _parse_labels(ver["labels"] or ""),
        "commit_message": ver.get("commit_message"),
        "created_at": ver.get("created_at"),
    }
