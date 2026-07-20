"""harness_snapshots 数据访问层 + 发布聚合（Phase 8 compose 配置化重构）。

职责：
  - harness_snapshots 表 CRUD（配置快照，不可变）
  - production 查询（执行端 loader 主入口）
  - 发布聚合（存 config_json + source_commit → 新 production → 旧 production 降 retired）

Phase 8 变更（决策 #18，替代 Phase 7 tar 整包）：
  - 版本化对象从 tar_blob（整包源码 tar）→ config_json（HarnessConfig JSON）
  - schema_lock 废弃（决策 #12，重跑式 A/B 无 replay 需求）
  - source_commit 新增：对应 git commit hash（executor pull 源码用，决策 D7a）
  - 源码版本由 Git bare repo 管理（决策 D10b），不再 tar 进 DB

设计依据：设计文档 D7a/D10b/#18。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.snapshot_repo")

STATUS_PRODUCTION = "production"
STATUS_RETIRED = "retired"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 查询 ─────────────────────────────────────────────────────


def get_snapshot(version: int) -> dict[str, Any] | None:
    """按 version 精确取快照（只返回有 config_json 的有效行）。"""
    row = db.query_one(
        "SELECT * FROM harness_snapshots WHERE version=? AND config_json IS NOT NULL",
        (version,),
    )
    return dict(row) if row else None


def get_production_snapshot() -> dict[str, Any] | None:
    """取当前 production 快照（执行端 loader 主入口）。

    只返回有 config_json 的有效行（过滤掉老 tar 快照退役行）。
    同一时刻只有一个 status='production'。无则返回 None。
    """
    row = db.query_one(
        """SELECT * FROM harness_snapshots
           WHERE status=? AND config_json IS NOT NULL
           ORDER BY version DESC LIMIT 1""",
        (STATUS_PRODUCTION,),
    )
    return dict(row) if row else None


def list_snapshots(*, status: str | None = None) -> list[dict[str, Any]]:
    """列快照（按版本倒序，只含有 config_json 的有效行）。可按 status 过滤。"""
    if status:
        rows = db.query_all(
            """SELECT * FROM harness_snapshots
               WHERE status=? AND config_json IS NOT NULL
               ORDER BY version DESC""",
            (status,),
        )
    else:
        rows = db.query_all(
            """SELECT * FROM harness_snapshots
               WHERE config_json IS NOT NULL ORDER BY version DESC"""
        )
    return [dict(r) for r in rows]


def get_snapshot_config(version: int) -> dict | None:
    """取快照的 config_json 解析为 dict。不存在/无效返回 None。"""
    row = db.query_one(
        "SELECT config_json FROM harness_snapshots WHERE version=? AND config_json IS NOT NULL",
        (version,),
    )
    if not row:
        return None
    return json.loads(row["config_json"])


def get_snapshot_source_commit(version: int) -> str | None:
    """取快照的 source_commit（git commit hash）。不存在返回 None。"""
    row = db.query_one(
        "SELECT source_commit FROM harness_snapshots WHERE version=?",
        (version,),
    )
    return row["source_commit"] if row else None


def next_version() -> int:
    """取下一个可用版本号（当前最大 version + 1，无行则 1）。"""
    row = db.query_one(
        "SELECT MAX(version) AS max_v FROM harness_snapshots WHERE config_json IS NOT NULL"
    )
    if not row or row["max_v"] is None:
        return 1
    return row["max_v"] + 1


# ── 发布聚合（决策 #18：存 config_json + source_commit）─────────


def publish_config(
    config: dict,
    *,
    source_commit: str | None = None,
    parent_version: int | None = None,
    change_summary: str | None = None,
    source_session: str | None = None,
) -> dict[str, Any]:
    """存 config_json → 发布新 production 快照。

    全局锁（沿用 manifest_repo D12 模式）：
      1. 序列化 config → config_json
      2. 取 db._lock（全局写锁）
      3. INSERT 新快照（status=production），旧 production 降 retired
      4. 提交
      5. 计算 v(parent)→v(new) 的 config diff，写入 version_changes（异常不阻断）

    Args:
        config:         HarnessConfig dict（会 validate + 序列化）
        source_commit:  对应 git commit hash（executor pull 源码用，决策 D7a）
        parent_version: 谱系（默认取当前 production 版本号）
        change_summary: 本版改了哪些
        source_session: 产出该版本的 evolve session_id（建立 session→version 映射）

    Returns: 新 production 快照行。
    """
    # 延迟 import 避免循环（harness_config 包可能间接依赖本模块）
    from app.harness_config import config as cfg

    config_json = cfg.to_json(config)  # 含 validate

    with db._lock:  # noqa: SLF001（全局锁是 db 模块的契约）
        # 谱系：默认接当前 production
        if parent_version is None:
            cur_prod = get_production_snapshot()
            parent_version = cur_prod["version"] if cur_prod else None

        # 新版本号
        next_v = next_version()

        # 旧 production 降 retired（在同一锁内，同时刻只有一个 production）
        db.execute(
            "UPDATE harness_snapshots SET status=? WHERE status=?",
            (STATUS_RETIRED, STATUS_PRODUCTION),
        )

        db.execute(
            """INSERT INTO harness_snapshots
               (version, parent_version, config_json, source_commit,
                change_summary, source_session, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (next_v, parent_version, config_json, source_commit,
             change_summary, source_session, STATUS_PRODUCTION, _now()),
        )
        result = get_snapshot(next_v)
        logger.info(
            "发布 production 配置快照 v%s（config %d 字符, commit=%s）",
            next_v, len(config_json), source_commit or "N/A",
        )

    # 计算 config diff 并存库（版本差异展示功能，D-T10）。
    # 放在锁外，异常不阻断发版（需求 D13）。
    _compute_and_save_diff(next_v, parent_version, config)

    return result


def _compute_and_save_diff(
    new_version: int,
    parent_version: int | None,
    new_config: dict,
) -> None:
    """计算 v(parent)→v(new) 的 config diff，写入 version_changes。

    parent 不存在或 config 残缺 → 跳过（需求 D13 异常处理）。
    计算异常 → 记日志，不阻断发版。
    """
    from app.versioning import config_diff, version_changes_repo

    if parent_version is None:
        logger.info("v%s 无 parent_version，跳过 diff 计算（首版）", new_version)
        return

    try:
        parent_config = get_snapshot_config(parent_version)
        if parent_config is None:
            logger.warning(
                "v%s 的 parent v%s 无 config_json，跳过 diff 计算", new_version, parent_version
            )
            return

        agent_diffs = config_diff.compute_diff(parent_config, new_config)
        if config_diff.has_changes(agent_diffs):
            version_changes_repo.save_agent_diffs(new_version, agent_diffs)
            logger.info(
                "v%s diff 计算完成：%d 个 agent 有变化", new_version, len(agent_diffs)
            )
        else:
            logger.info("v%s 与 parent v%s 无 config 差异", new_version, parent_version)
    except Exception:
        logger.exception("v%s diff 计算失败（不阻断发版）", new_version)


# ── 兼容层（Phase 7 tar 快照查询，供过渡期 ab_runner 用）──────────


def get_snapshot_tar(version: int) -> bytes | None:
    """[已废弃] 取老 tar 快照的 tar_blob。Phase 8 后新快照无 tar_blob。

    保留只为过渡期兼容（老快照行仍可查 tar）。新代码应改用 get_snapshot_config。
    """
    row = db.query_one(
        "SELECT tar_blob FROM harness_snapshots WHERE version=?",
        (version,),
    )
    return row["tar_blob"] if row else None


def publish_production(
    package_dir: Path,
    *,
    parent_version: int | None = None,
    change_summary: str | None = None,
) -> dict[str, Any] | None:
    """[已废弃] Phase 7 的 tar 整包发布。Phase 8 后请改用 publish_config。

    保留签名只为过渡期兼容（若旧调用方未迁移）。内部转发到 bootstrap + publish_config。
    """
    logger.warning("publish_production(tar) 已废弃，请改用 publish_config(config)")
    from app.harness_config.bootstrap import build_v1_config

    config = build_v1_config()
    return publish_config(
        config,
        source_commit=None,
        parent_version=parent_version,
        change_summary=change_summary or "Phase 8 迁移：tar→config（bootstrap 生成 v1）",
    )


__all__ = [
    "STATUS_PRODUCTION",
    "STATUS_RETIRED",
    "get_snapshot",
    "get_production_snapshot",
    "list_snapshots",
    "get_snapshot_config",
    "get_snapshot_source_commit",
    "next_version",
    "publish_config",
    "get_snapshot_tar",
    "publish_production",
]
