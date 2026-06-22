"""harness manifest 数据访问层 + 发布聚合（Phase 6 T1.4）。

职责：
  - harness_manifests 表 CRUD（部署快照）
  - production 查询（执行端 loader 主入口）
  - 发布聚合（D7：approved 聚合产物 + D12 全局锁快照）

manifest 是「部署单元」：一份 manifest = 各 surface 当前 approved 版本的指针聚合。
执行端按 manifest 逐 surface 加载，装配成完整 harness（替代硬编码 v1）。

manifest 不是被编辑对象（D7）——它由 publish_production 从 approved surface 聚合生成。
同一时刻只有一个 status='production' 的 manifest。

schema_lock（回放契约，决策 D3/D11）：
  - 记录该 manifest 用了哪些 C 类 surface 及版本（c_surfaces: [{name, version}]）
  - 回放老 trace 时，校验 trace 当时的 C 类版本与重放用 manifest 的 c_surfaces 一致
  - channel 全集不在 manifest 里（那是 C 类代码内部细节），由执行端 importlib
    加载 C 类后从真实 state_schema 聚合（D11 进程启动加载）

设计依据：设计文档 D7（approved 聚合）+ D12（全局锁快照）+ D3（C 类 schema）。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.improvement import surface_registry, surface_repo

logger = logging.getLogger("evolution.manifest_repo")

STATUS_DRAFT = "draft"
STATUS_PRODUCTION = "production"
STATUS_RETIRED = "retired"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 查询 ─────────────────────────────────────────────────────


def get_manifest(manifest_version: int) -> dict[str, Any] | None:
    """按 manifest_version 精确取。"""
    return db.query_one(
        "SELECT * FROM harness_manifests WHERE manifest_version=?",
        (manifest_version,),
    )


def get_production_manifest() -> dict[str, Any] | None:
    """取当前 production manifest（执行端 loader 主入口）。

    同一时刻只有一个 status='production'。无则返回 None。
    """
    return db.query_one(
        "SELECT * FROM harness_manifests WHERE status=? ORDER BY manifest_version DESC LIMIT 1",
        (STATUS_PRODUCTION,),
    )


def list_manifests(*, status: str | None = None) -> list[dict[str, Any]]:
    """列 manifest（按版本倒序）。可按 status 过滤。"""
    if status:
        return db.query_all(
            "SELECT * FROM harness_manifests WHERE status=? ORDER BY manifest_version DESC",
            (status,),
        )
    return db.query_all(
        "SELECT * FROM harness_manifests ORDER BY manifest_version DESC"
    )


def get_entries(manifest: dict[str, Any]) -> dict[str, Any]:
    """解析 manifest 的 entries_json（含 surfaces + schema_lock）。"""
    return json.loads(manifest["entries_json"])


# ── 发布聚合（D7 + D12）─────────────────────────────────────


def publish_production(
    *,
    parent_version: int | None = None,
    change_summary: str | None = None,
) -> dict[str, Any] | None:
    """聚合当前所有 approved surface → 生成新 production manifest。

    全局锁快照（D12）：
      1. 取 db._lock（全局写锁，与 init_db/execute 同一把，保证聚合期间无 surface 状态变更）
      2. 一次性 SELECT 所有 approved 最高版本（surface_repo.list_all_approved_grouped）
      3. 构造 entries_json（surfaces + schema_lock）
      4. INSERT 新 manifest（status=production），旧 production 降 retired
      5. 提交

    Args:
        parent_version: 谱系（默认取当前 production 版本号）
        change_summary: 本版改了哪些 surface（相对 parent）

    Returns: 新 production manifest 行，或 None（无任何 approved surface 时）。
    """
    # ── 全局锁快照：聚合期间冻结 surface 状态 ──
    # db._lock 是 RLock（与 execute/init_db 共用），持锁期间无并发写 surface_versions。
    with db._lock:  # noqa: SLF001（全局锁是 db 模块的契约）
        approved = surface_repo.list_all_approved_grouped()
        if not approved:
            logger.warning("发布失败：无任何 approved surface，无法聚合 manifest")
            return None

        entries = _build_entries(approved)

        # 谱系：默认接当前 production
        if parent_version is None:
            cur_prod = get_production_manifest()
            parent_version = cur_prod["manifest_version"] if cur_prod else None

        # change_summary：未提供时自动算（与 parent 的 surface 版本 diff）
        if change_summary is None and parent_version is not None:
            change_summary = _diff_against_parent(parent_version, entries)
        elif change_summary is None:
            change_summary = "初始 manifest（从 v1 迁移）"

        # 新版本号
        latest = db.query_one("SELECT MAX(manifest_version) AS mv FROM harness_manifests")
        next_version = (latest["mv"] or 0) + 1 if latest and latest["mv"] else 1

        # 旧 production 降 retired（在同一锁内，保证同时刻只有一个 production）
        db.execute(
            "UPDATE harness_manifests SET status=? WHERE status=?",
            (STATUS_RETIRED, STATUS_PRODUCTION),
        )

        db.execute(
            """INSERT INTO harness_manifests
               (manifest_version, parent_version, entries_json, status, change_summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (next_version, parent_version, json.dumps(entries, ensure_ascii=False),
             STATUS_PRODUCTION, change_summary, _now()),
        )
        result = get_manifest(next_version)
        logger.info("发布 production manifest v%s（%d surfaces）",
                    next_version, len(entries["surfaces"]))
        return result


def _build_entries(approved: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """从 approved 版本分组构造 entries_json 结构。

    结构（见设计文档接口契约）：
      {
        "surfaces": [
          {"surface_type", "surface_name", "scope", "version", "id"}, ...
        ],
        "schema_lock": {
          "c_surfaces": [{"surface_name", "version"}, ...]  # C 类版本指针（回放契约）
        }
      }
    """
    surfaces: list[dict[str, Any]] = []
    c_surfaces: list[dict[str, Any]] = []
    for (surface_type, surface_name), row in sorted(approved.items()):
        entry = {
            "surface_type": surface_type,
            "surface_name": surface_name,
            "scope": row["scope"],
            "version": row["version"],
            "id": row["id"],
        }
        surfaces.append(entry)
        # C 类单独记入 schema_lock（回放契约锁定其版本）
        if surface_registry.is_c_code(surface_type):
            c_surfaces.append({
                "surface_name": surface_name,
                "version": row["version"],
            })
    return {
        "surfaces": surfaces,
        "schema_lock": {"c_surfaces": c_surfaces},
    }


def _diff_against_parent(parent_version: int, new_entries: dict[str, Any]) -> str:
    """算新 entries 相对 parent manifest 改了哪些 surface（人话摘要）。"""
    parent = get_manifest(parent_version)
    if parent is None:
        return f"基于 parent v{parent_version}（已不存在）"
    parent_map: dict[tuple[str, str], int] = {
        (s["surface_type"], s["surface_name"]): s["version"]
        for s in get_entries(parent)["surfaces"]
    }
    changes: list[str] = []
    for s in new_entries["surfaces"]:
        key = (s["surface_type"], s["surface_name"])
        old_v = parent_map.get(key)
        if old_v is None:
            changes.append(f"+{s['surface_name']}(v{s['version']})")
        elif old_v != s["version"]:
            changes.append(f"~{s['surface_name']}(v{old_v}→v{s['version']})")
    return ", ".join(changes) if changes else "无变化（重新发布）"


# ── 回放契约校验（供执行端 worker A/B 回放用）──────────────


def check_replay_compatible(
    replay_manifest: dict[str, Any],
    trace_c_surfaces: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """校验回放用的 manifest 与 trace 当时的 C 类版本是否一致。

    Args:
        replay_manifest: 回放用的 manifest 行
        trace_c_surfaces: trace 记录的当时 C 类 surface 版本 [{surface_name, version}]

    Returns: (compatible, mismatch_descriptions)。
    C 类改动 State schema，版本不一致 → 回放失真 → 必须拦截。
    A/B 类无此约束（它们不改 schema）。
    """
    replay_entries = get_entries(replay_manifest)
    replay_c = {
        s["surface_name"]: s["version"]
        for s in replay_entries["schema_lock"]["c_surfaces"]
    }
    trace_c = {s["surface_name"]: s["version"] for s in trace_c_surfaces}

    mismatches: list[str] = []
    # replay 多出的 C 类（trace 时还没有）
    for name, ver in replay_c.items():
        if name not in trace_c:
            mismatches.append(f"C 类 {name}(v{ver}) 在 trace 时不存在")
    # trace 有的 C 类，版本不一致
    for name, ver in trace_c.items():
        if name not in replay_c:
            mismatches.append(f"C 类 {name}(v{ver}) 在回放 manifest 中缺失")
        elif replay_c[name] != ver:
            mismatches.append(
                f"C 类 {name} 版本不一致: trace=v{ver} vs replay=v{replay_c[name]}"
            )
    return len(mismatches) == 0, mismatches
