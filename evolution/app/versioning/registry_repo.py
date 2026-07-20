"""registry_repo —— harness 版本注册表读写层（去 DB 重构）。

替代 snapshot_repo 的所有 DB 查询功能。数据源从 SQLite harness_snapshots 表
换成 harness 独立仓库内的 registry.json 文件。

设计依据：设计文档 20260713_003000（去 DB 轻量化重构）。

registry.json 结构（快照式：versions 数组 + production 指针 + rollback_log）：
  {
    "schema_version": 1,
    "production": <version>,          # 当前生产版本号
    "versions": [ { version, commit, parent_version, change_summary,
                    eval_score, eval_status, created_at, source_session, ... } ],
    "rollback_log": [ { at, from, to, reason } ]
  }

版本内容真相源 = git（commit）；元信息真相源 = registry.json。
两者在同一个 git commit 里（单 commit 原子性），executor pull main 时一并拿到。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.settings import settings

logger = logging.getLogger("evolution.registry_repo")

STATUS_PRODUCTION = "production"
STATUS_RETIRED = "retired"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _registry_path() -> Path:
    """registry.json 路径（在 harness 工作目录 repo/ 下）。"""
    return settings.harness_work_dir_path / "registry.json"


def _read() -> dict[str, Any]:
    """读取 registry.json。文件不存在返回空骨架。"""
    path = _registry_path()
    if not path.exists():
        logger.warning("registry.json 不存在: %s，返回空骨架", path)
        return {"schema_version": 1, "production": None, "versions": [], "rollback_log": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write(data: dict[str, Any]) -> None:
    """写入 registry.json（覆盖式，git 天然保留历史）。"""
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 查询（对齐 snapshot_repo 旧接口语义，调用者改动最小）──


def _with_status(version_entry: dict[str, Any], prod_version: int | None) -> dict[str, Any]:
    """给版本条目补 status 字段（兼容旧 DB 的 production/retired 语义）。

    registry 用 production 指针而非 per-version status，但调用者/前端期望
    status 字段。这里动态计算：等于 production 指针 → "production"，否则 "retired"。
    """
    entry = dict(version_entry)
    entry["status"] = (
        STATUS_PRODUCTION if entry["version"] == prod_version else STATUS_RETIRED
    )
    return entry


def get_version(version: int) -> dict[str, Any] | None:
    """取指定版本的元数据（含动态计算的 status）。不存在返回 None。"""
    data = _read()
    prod_v = data.get("production")
    for v in data["versions"]:
        if v["version"] == version:
            return _with_status(v, prod_v)
    return None


def get_production_version() -> dict[str, Any] | None:
    """取当前 production 版本的元数据。无则 None。"""
    data = _read()
    prod_v = data.get("production")
    if prod_v is None:
        return None
    for v in data["versions"]:
        if v["version"] == prod_v:
            return _with_status(v, prod_v)
    logger.warning("production 指向 v%s 但该版本不在 versions 里", prod_v)
    return None


def list_versions() -> list[dict[str, Any]]:
    """列所有版本（按版本号倒序，含动态计算的 status）。"""
    data = _read()
    prod_v = data.get("production")
    versions = sorted(data["versions"], key=lambda v: v["version"], reverse=True)
    return [_with_status(v, prod_v) for v in versions]


def get_production_version_number() -> int | None:
    """取当前 production 版本号（轻量查询，只读指针）。"""
    return _read().get("production")


def get_version_commit(version: int) -> str | None:
    """取某版本对应的 git commit hash。

    registry 不存 commit hash（自引用问题），通过 git log 顺序映射：
    version N = git log 倒序第 N 个 commit。
    """
    from app.core import git_ops

    v = get_version(version)
    if v is None:
        return None
    # git log 倒序（最新在前），version 1 = 最早的 commit
    log = git_ops.log_oneline()
    commits = [line.split()[0] for line in log if line.strip()]
    # version 编号从 1 开始，commit 倒序后最老的在最后
    if 1 <= version <= len(commits):
        return commits[len(commits) - version]
    return None


# ── 写入（发布 / 回滚）──


def _next_version_number(data: dict[str, Any]) -> int:
    """取下一个可用版本号（当前最大 + 1，无则 1）。"""
    if not data["versions"]:
        return 1
    return max(v["version"] for v in data["versions"]) + 1


def publish_version(
    *,
    change_summary: str | None = None,
    source_session: str | None = None,
    eval_score: float | None = None,
    eval_status: str = "pending",
) -> dict[str, Any]:
    """发布新 production 版本（更新 registry.json，不负责 git commit）。

    操作：
      1. 读 registry
      2. 新版本号 = max + 1，parent = 当前 production
      3. append 新版本条目（不含 commit hash——自引用问题见下方说明）
      4. production 指针移到新版本

    commit hash 不在 registry 记录：registry 和源码在同一个 git commit 里，
    记录"自身所在 commit 的 hash"是自引用（git 计算时 registry 内容还没填 hash）。
    version↔commit 映射通过 git log 顺序推导（第 N 次 publish 的 commit = version N），
    需要精确 hash 时调 git_ops（current_commit / show_file 等）。

    调用方负责在调本函数后，把 registry 变更和源码改动放在同一个 git commit 里
    （单 commit 原子性：git_ops.commit_and_push）。

    Args:
        change_summary: 本版改了什么
        source_session: 产出该版本的 evolve session_id
        eval_score:     评估分（eval 成熟后填）
        eval_status:    评估状态 pending|passed|failed

    Returns: 新版本条目 dict。
    """
    data = _read()
    cur_prod = data.get("production")
    next_v = _next_version_number(data)

    entry = {
        "version": next_v,
        "parent_version": cur_prod,
        "change_summary": change_summary,
        "eval_score": eval_score,
        "eval_status": eval_status,
        "created_at": _now(),
        "source_session": source_session,
    }
    data["versions"].append(entry)
    data["production"] = next_v
    _write(data)
    logger.info("发布 production v%s", next_v)
    return entry


def rollback(to_version: int, *, reason: str | None = None) -> dict[str, Any]:
    """回滚 production 指针到指定历史版本。

    回滚 = 移动 production 指针 + 记 rollback_log。
    不删版本、不改 git 历史（谱系完整）。
    实际让 executor 生效需要 git revert 或 checkout（由调用方操作 git）。

    Args:
        to_version: 回退到哪个版本
        reason:     回滚原因

    Returns: 回滚后的 production 版本条目。
    """
    data = _read()
    target = None
    for v in data["versions"]:
        if v["version"] == to_version:
            target = v
            break
    if target is None:
        raise ValueError(f"版本 v{to_version} 不存在于 registry")

    from_version = data.get("production")
    data["production"] = to_version
    data["rollback_log"].append({
        "at": _now(),
        "from": from_version,
        "to": to_version,
        "reason": reason,
    })
    _write(data)
    logger.info("回滚 production: v%s → v%s（%s）", from_version, to_version, reason or "无原因")
    return dict(target)


def update_version_meta(
    version: int,
    *,
    eval_score: float | None = None,
    eval_status: str | None = None,
    change_summary: str | None = None,
) -> dict[str, Any] | None:
    """更新某版本的元数据（如回填评估分 / change_summary）。

    用于 eval 门控成熟后回填评估结果，或发布后补全信息。
    """
    data = _read()
    for v in data["versions"]:
        if v["version"] == version:
            if eval_score is not None:
                v["eval_score"] = eval_score
            if eval_status is not None:
                v["eval_status"] = eval_status
            if change_summary is not None:
                v["change_summary"] = change_summary
            _write(data)
            return dict(v)
    logger.warning("update_version_meta: v%s 不存在", version)
    return None


__all__ = [
    "get_version",
    "get_production_version",
    "list_versions",
    "get_production_version_number",
    "get_version_commit",
    "publish_version",
    "rollback",
    "update_version_meta",
    "STATUS_PRODUCTION",
    "STATUS_RETIRED",
]
