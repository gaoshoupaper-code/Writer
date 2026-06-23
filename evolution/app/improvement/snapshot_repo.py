"""harness_snapshots 数据访问层 + 发布聚合（Phase 7 T0.3，取代 manifest_repo）。

职责：
  - harness_snapshots 表 CRUD（整包快照，不可变）
  - production 查询（执行端 loader 主入口）
  - 发布聚合（tar 整包目录 → 存快照 → 旧 production 降 retired）

与 manifest_repo 的区别（Phase 7 包化重构）：
  - manifest_repo：聚合 approved surface 指针 → entries_json（surface 级版本）
  - snapshot_repo：tar 整个包目录 → tar_blob（整包单版本，D6=①）
  - 版本粒度从 surface 级变整包级；content 不在 DB（在包目录）

schema_lock（回放契约，沿用 Phase 6 设计）：
  - 从包内 manifest.json 读 schema_lock.c_surfaces（C 类 surface 名+版本）
  - 回放老 trace 时校验版本一致（C 类改 state_schema，不一致 → 回放失真 → 拦截）

设计依据：设计文档 D6=①（整包单版本）+ D10=b1（废弃旧表）+ Q9=iii（快照存 DB）。
"""
from __future__ import annotations

import io
import json
import logging
import tarfile
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
    """按 version 精确取快照。"""
    return db.query_one(
        "SELECT * FROM harness_snapshots WHERE version=?",
        (version,),
    )


def get_production_snapshot() -> dict[str, Any] | None:
    """取当前 production 快照（执行端 loader 主入口）。

    同一时刻只有一个 status='production'。无则返回 None。
    """
    return db.query_one(
        "SELECT * FROM harness_snapshots WHERE status=? ORDER BY version DESC LIMIT 1",
        (STATUS_PRODUCTION,),
    )


def list_snapshots(*, status: str | None = None) -> list[dict[str, Any]]:
    """列快照（按版本倒序）。可按 status 过滤。"""
    if status:
        return db.query_all(
            "SELECT * FROM harness_snapshots WHERE status=? ORDER BY version DESC",
            (status,),
        )
    return db.query_all(
        "SELECT * FROM harness_snapshots ORDER BY version DESC"
    )


def get_snapshot_tar(version: int) -> bytes | None:
    """取快照的 tar_blob（A/B 解压/回放用）。不存在返回 None。"""
    row = db.query_one(
        "SELECT tar_blob FROM harness_snapshots WHERE version=?",
        (version,),
    )
    return row["tar_blob"] if row else None


# ── 发布聚合（D6=① + Q9=iii）──────────────────────────────


def publish_production(
    package_dir: Path,
    *,
    parent_version: int | None = None,
    change_summary: str | None = None,
) -> dict[str, Any] | None:
    """tar 整包目录 → 发布新 production 快照。

    全局锁快照（沿用 manifest_repo D12 模式）：
      1. 取 db._lock（全局写锁）
      2. tar package_dir → tar_blob（内存，不含 __pycache__/.git）
      3. 读 package_dir/manifest.json 取 schema_lock + version
      4. INSERT 新快照（status=production），旧 production 降 retired
      5. 提交

    Args:
        package_dir: Agent 包目录（evolution/harnesses/current/）。
        parent_version: 谱系（默认取当前 production 版本号）。
        change_summary: 本版改了哪些文件（未提供时从 manifest.json 读）。

    Returns: 新 production 快照行，或 None（package_dir 无 manifest.json 时）。
    """
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.exists():
        logger.error("发布失败：包目录无 manifest.json: %s", package_dir)
        return None

    with open(manifest_path, encoding="utf-8") as f:
        manifest_meta = json.load(f)

    schema_lock = json.dumps(manifest_meta.get("schema_lock", {}), ensure_ascii=False)

    with db._lock:  # noqa: SLF001（全局锁是 db 模块的契约）
        # tar 整包目录（内存，排除缓存）
        tar_blob = _tar_package(package_dir)

        # 谱系：默认接当前 production
        if parent_version is None:
            cur_prod = get_production_snapshot()
            parent_version = cur_prod["version"] if cur_prod else None

        # change_summary：未提供时从 manifest.json 读
        if change_summary is None:
            change_summary = manifest_meta.get("change_summary")

        # 新版本号：包内 manifest.json 的 version 是真理源（D6=① 整包版本）
        next_version = manifest_meta["version"]

        # 旧 production 降 retired（在同一锁内，同时刻只有一个 production）
        db.execute(
            "UPDATE harness_snapshots SET status=? WHERE status=?",
            (STATUS_RETIRED, STATUS_PRODUCTION),
        )

        db.execute(
            """INSERT INTO harness_snapshots
               (version, parent_version, tar_blob, tar_size, schema_lock,
                change_summary, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (next_version, parent_version, tar_blob, len(tar_blob), schema_lock,
             change_summary, STATUS_PRODUCTION, _now()),
        )
        result = get_snapshot(next_version)
        logger.info(
            "发布 production 快照 v%s（tar %d bytes）",
            next_version, len(tar_blob),
        )
        return result


def _tar_package(package_dir: Path) -> bytes:
    """把包目录打包成 tar（内存 bytes，排除 __pycache__/.git/.pyc）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            # 排除缓存/版本控制文件
            parts = path.relative_to(package_dir).parts
            if any(p in ("__pycache__", ".git", ".pytest_cache") for p in parts):
                continue
            if path.suffix == ".pyc":
                continue
            tar.add(path, arcname=str(path.relative_to(package_dir)))
    return buf.getvalue()


# ── 回放契约校验（供执行端 A/B 回放用）──────────────────────


def check_replay_compatible(
    replay_snapshot: dict[str, Any],
    trace_c_surfaces: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """校验回放用快照与 trace 当时的 C 类版本是否一致。

    沿用 Phase 6 manifest_repo.check_replay_compatible 的逻辑，数据源改为快照 schema_lock。
    """
    replay_schema_lock = json.loads(replay_snapshot["schema_lock"])
    replay_c = {
        (s["name"], s.get("scope", "")): s["version"]
        for s in replay_schema_lock.get("c_surfaces", [])
    }
    trace_c = {
        (s["name"], s.get("scope", "")): s["version"]
        for s in trace_c_surfaces
    }

    mismatches: list[str] = []
    for (name, scope), ver in replay_c.items():
        if (name, scope) not in trace_c:
            mismatches.append(f"C 类 {name}/{scope}(v{ver}) 在 trace 时不存在")
    for (name, scope), ver in trace_c.items():
        if (name, scope) not in replay_c:
            mismatches.append(f"C 类 {name}/{scope}(v{ver}) 在回放快照中缺失")
        elif replay_c[(name, scope)] != ver:
            mismatches.append(
                f"C 类 {name}/{scope} 版本不一致: trace=v{ver} vs replay=v{replay_c[(name, scope)]}"
            )
    return len(mismatches) == 0, mismatches
