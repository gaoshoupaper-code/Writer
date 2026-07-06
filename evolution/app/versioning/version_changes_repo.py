"""version_changes 数据访问层（版本差异展示功能）。

version_changes 表存 publish 时算好的 config diff（按 agent 聚合 + JSON 明细）。
两种行：
  - agent 级行（agent = meta_pipeline/storybuilding/...）：diff_json 存三要素 diff
  - 版本级行（agent = '__version__'）：intent_json 存 design_doc 意图列表

设计依据：设计文档 D-T4（schema）/ D-T5（版本级独立行存意图）。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.version_changes_repo")

# 版本级行的特殊 agent 名（存 design_doc 意图，不对应具体 agent）
VERSION_LEVEL_AGENT = "__version__"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 写入 ───────────────────────────────────────────────────────────


def save_agent_diffs(version: int, agent_diffs: dict[str, dict[str, Any]]) -> None:
    """写入某版本的 agent 级 diff 行（批量）。

    每个 agent 一行，diff_json 存该 agent 的三要素 diff 明细。
    若该版本已有行，先删后写（保证幂等，供回填脚本复用）。

    Args:
        version:       版本号
        agent_diffs:   {agent_name: diff_dict}（来自 config_diff.compute_diff）
    """
    now = _now()
    conn = db.get_conn()
    with db._lock:  # noqa: SLF001（全局锁是 db 模块的契约）
        # 先删该版本的旧 agent 级行（保留版本级行），再批量写入
        conn.execute(
            "DELETE FROM version_changes WHERE version=? AND agent != ?",
            (version, VERSION_LEVEL_AGENT),
        )
        for agent_name, diff in agent_diffs.items():
            conn.execute(
                """INSERT OR REPLACE INTO version_changes
                   (version, agent, diff_json, intent_json, computed_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (version, agent_name, json.dumps(diff, ensure_ascii=False), now),
            )
        conn.commit()


def save_intent(version: int, intent: list[dict[str, Any]]) -> None:
    """写入某版本的版本级意图行（design_doc 的 changes 列表）。

    存为 agent='__version__' 的单行，intent_json 存意图列表。
    幂等：INSERT OR REPLACE。

    Args:
        version: 版本号
        intent:  design_doc 的 changes 列表（每条含 target/change_desc/reason/expected_up/expected_down）
    """
    now = _now()
    conn = db.get_conn()
    with db._lock:  # noqa: SLF001
        conn.execute(
            """INSERT OR REPLACE INTO version_changes
               (version, agent, diff_json, intent_json, computed_at)
               VALUES (?, ?, NULL, ?, ?)""",
            (version, VERSION_LEVEL_AGENT, json.dumps(intent, ensure_ascii=False), now),
        )
        conn.commit()


# ── 查询 ───────────────────────────────────────────────────────────


def get_changes(version: int) -> dict[str, Any]:
    """取某版本的完整 changes（agent 级 diff + 版本级意图）。

    Returns:
        {
            "agents": [{"agent": name, "diff": {...}}, ...],  # 按 agent 名排序
            "intent": [...] | None,                            # design_doc 意图；无则 None
        }
        无任何 changes 行 → {"agents": [], "intent": None}。
    """
    rows = db.query_all(
        "SELECT agent, diff_json, intent_json FROM version_changes WHERE version=? ORDER BY agent",
        (version,),
    )
    if not rows:
        return {"agents": [], "intent": None}

    agents: list[dict[str, Any]] = []
    intent: list[dict[str, Any]] | None = None
    for r in rows:
        agent = r["agent"]
        if agent == VERSION_LEVEL_AGENT:
            if r["intent_json"]:
                try:
                    intent = json.loads(r["intent_json"])
                except json.JSONDecodeError:
                    logger.warning("version_changes v%s intent_json 解析失败", version)
                    intent = None
        else:
            if r["diff_json"]:
                try:
                    agents.append({"agent": agent, "diff": json.loads(r["diff_json"])})
                except json.JSONDecodeError:
                    logger.warning("version_changes v%s agent=%s diff_json 解析失败", version, agent)

    return {"agents": agents, "intent": intent}


def has_diff(version: int) -> bool:
    """该版本是否已有 agent 级 diff 行。"""
    row = db.query_one(
        "SELECT 1 FROM version_changes WHERE version=? AND agent != ? LIMIT 1",
        (version, VERSION_LEVEL_AGENT),
    )
    return row is not None


def list_versions_with_diffs() -> list[int]:
    """列出有 agent 级 diff 行的版本号（升序）。供回填脚本判断幂等。"""
    rows = db.query_all(
        "SELECT DISTINCT version FROM version_changes WHERE agent != ? ORDER BY version",
        (VERSION_LEVEL_AGENT,),
    )
    return [r["version"] for r in rows] if rows else []


__all__ = [
    "VERSION_LEVEL_AGENT",
    "save_agent_diffs",
    "save_intent",
    "get_changes",
    "has_diff",
    "list_versions_with_diffs",
]
