"""回填脚本：为已存在的版本计算 config diff + 提取意图，灌入 version_changes。

用法（在 evolution/ 目录下）：
    python -m scripts.backfill_version_changes            # 幂等，跳过已有 diff 的版本
    python -m scripts.backfill_version_changes --force    # 强制重算所有版本

幂等：version_changes 已有该 version 的 agent 级行 → 跳过（除非 --force）。
v1（无 parent_version）跳过 diff 计算。

设计依据：设计文档 D-T11（回填脚本）+ 需求 D8（手动回填）。
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.core import db
from app.evolve.docs import parse_design_doc_intent
from app.versioning import config_diff, snapshot_repo, version_changes_repo

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger("backfill")


def backfill(force: bool = False) -> None:
    """主回填逻辑。"""
    db.init_db()

    # 取所有有效快照（有 config_json），按版本升序
    snaps = snapshot_repo.list_snapshots()
    snaps_ascending = sorted(snaps, key=lambda s: s["version"])
    logger.info("发现 %d 个版本（含 config_json）", len(snaps_ascending))

    already_done = set() if force else set(version_changes_repo.list_versions_with_diffs())
    if already_done and not force:
        logger.info("已存在 diff 的版本（将跳过）：%s", sorted(already_done))

    diff_count = 0
    skip_count = 0
    intent_count = 0

    for snap in snaps_ascending:
        version = snap["version"]
        parent_version = snap.get("parent_version")
        source_session = snap.get("source_session")

        # ── 1. 回填 agent 级 diff ──
        if version in already_done and not force:
            skip_count += 1
        elif parent_version is None:
            logger.info("v%s 跳过：无 parent（首版）", version)
            skip_count += 1
        else:
            parent_config = snapshot_repo.get_snapshot_config(parent_version)
            if parent_config is None:
                logger.warning("v%s 跳过：parent v%s 无 config_json", version, parent_version)
                skip_count += 1
            else:
                new_config = snapshot_repo.get_snapshot_config(version)
                agent_diffs = config_diff.compute_diff(parent_config, new_config)
                if config_diff.has_changes(agent_diffs):
                    version_changes_repo.save_agent_diffs(version, agent_diffs)
                    diff_count += 1
                    logger.info(
                        "v%s diff 已回填：%d 个 agent 有变化", version, len(agent_diffs)
                    )
                else:
                    logger.info("v%s 与 parent v%s 无差异", version, parent_version)

        # ── 2. 回填版本级意图（来自 design_doc）──
        if source_session:
            session = _get_session(source_session)
            if session and session.get("design_doc_path"):
                intent = parse_design_doc_intent(session["design_doc_path"])
                if intent:
                    version_changes_repo.save_intent(version, intent)
                    intent_count += 1
                    logger.info("v%s 意图已回填：%d 条改动", version, len(intent))

    logger.info(
        "回填完成：diff 回填 %d 版，跳过 %d 版，意图回填 %d 版",
        diff_count, skip_count, intent_count,
    )


def _get_session(session_id: str) -> dict | None:
    """查 evolve_sessions（避免循环 import，直接走 db）。"""
    row = db.query_one(
        "SELECT * FROM evolve_sessions WHERE session_id = ?",
        (session_id,),
    )
    return dict(row) if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 version_changes（config diff + 意图）")
    parser.add_argument(
        "--force", action="store_true",
        help="强制重算所有版本（默认幂等，跳过已有 diff 的版本）",
    )
    args = parser.parse_args()
    backfill(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
