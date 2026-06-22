"""surface 版本管理数据访问层（Phase 6 T1.3）。

职责：
  - surface_versions 表 CRUD（统一版本表，A/B/C 三类 surface 共用）
  - 版本管理：version 在同 (surface_type, surface_name) 下单调递增
  - status 流转：draft → static_checked → ab_testing → approved/rejected
  - 按 surface_type / scope / status 查询（proposer/manifest/A/B 各取所需）

与 prompts_repo 的关系（决策 D5：manifest 统一接管）：
  - 本表取代 prompt_versions 的职责（prompt 成为 surface_type='prompt'）。
  - 不再有 label 字段（production 由 manifest 指向决定，非 surface 自身 label）。
  - 只跟踪 status（approved = 该版本通过了 A/B，可被 manifest 聚合）。

设计依据：设计文档 D1（统一表）+ D4（scope 列）+ D5（manifest 统一接管）。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.improvement import surface_registry

logger = logging.getLogger("evolution.surface_repo")

# status 合法值（surface 版本生命周期）
STATUS_DRAFT = "draft"
STATUS_STATIC_CHECKED = "static_checked"
STATUS_AB_TESTING = "ab_testing"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

VALID_STATUSES = frozenset({
    STATUS_DRAFT, STATUS_STATIC_CHECKED, STATUS_AB_TESTING, STATUS_APPROVED, STATUS_REJECTED,
})


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 创建版本 ─────────────────────────────────────────────────


def create_version(
    surface_type: str,
    surface_name: str,
    scope: str,
    content: str,
    *,
    config: dict[str, Any] | None = None,
    commit_message: str | None = None,
    source: str = "manual",
    status: str = STATUS_DRAFT,
    parent_version: int | None = None,
    signature_id: int | None = None,
    proposer_meta: dict[str, Any] | None = None,
    static_check_passed: bool | None = None,
) -> dict[str, Any]:
    """创建新 surface 版本：version = 同线 max+1。

    Args:
        surface_type: 见 surface_registry（prompt/skill/.../stateful_middleware）
        surface_name: 具体名（如 writing_system / GoalMiddleware）
        scope: 归属 subagent（见 surface_registry.VALID_SCOPES）
        content: 版本正文（A=文本/B=JSON/C=受限 Python）
        config: 附属配置（如 model temperature），JSON 序列化存储
        commit_message: 版本说明
        source: manual/proposed/ab_winner/migrated
        status: 初始状态（默认 draft）
        parent_version: 进化谱系（从哪个版本来）
        signature_id: 针对哪个失败签名（proposed 时填）
        proposer_meta: proposer 元信息
        static_check_passed: 静态检查结果（C 类必填，A/B 可 None）

    Returns: 新版本行（dict）。
    """
    # 编译期类型安全：校验 surface_type/scope 合法
    type_def = surface_registry.get_type_def(surface_type)  # 未知 type 抛 KeyError
    surface_registry.validate_scope(scope)  # 未知 scope 抛 ValueError
    if status not in VALID_STATUSES:
        raise ValueError(f"非法 status: {status}。合法值: {sorted(VALID_STATUSES)}")

    # content_kind 由 surface_type 决定（编译期保证一致，不允许调用方覆盖）
    content_kind = type_def.content_kind.value

    # version 单调递增（同 surface 线内）
    latest = db.query_one(
        "SELECT MAX(version) AS mv FROM surface_versions "
        "WHERE surface_type=? AND surface_name=?",
        (surface_type, surface_name),
    )
    next_version = (latest["mv"] or 0) + 1 if latest and latest["mv"] else 1

    db.execute(
        """INSERT INTO surface_versions
           (surface_type, surface_name, scope, version, content, content_kind,
            config, commit_message, source, status, parent_version, signature_id,
            proposer_meta, static_check_passed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            surface_type, surface_name, scope, next_version, content, content_kind,
            json.dumps(config or {}, ensure_ascii=False),
            commit_message, source, status, parent_version, signature_id,
            json.dumps(proposer_meta or {}, ensure_ascii=False),
            1 if static_check_passed is True else (0 if static_check_passed is False else None),
            _now(),
        ),
    )
    return get_version(surface_type, surface_name, next_version)  # type: ignore[return-value]


# ── 查询 ─────────────────────────────────────────────────────


def get_version(surface_type: str, surface_name: str, version: int) -> dict[str, Any] | None:
    """按 (type, name, version) 精确取版本。"""
    return db.query_one(
        "SELECT * FROM surface_versions WHERE surface_type=? AND surface_name=? AND version=?",
        (surface_type, surface_name, version),
    )


def get_version_by_id(version_id: int) -> dict[str, Any] | None:
    """按主键 id 取版本（manifest entries 引用用）。"""
    return db.query_one("SELECT * FROM surface_versions WHERE id=?", (version_id,))


def list_versions(
    surface_type: str,
    surface_name: str,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """列某条 surface 线的所有版本（按 version 倒序）。

    可按 status 过滤（如只看 approved）。
    """
    if status:
        return db.query_all(
            "SELECT * FROM surface_versions "
            "WHERE surface_type=? AND surface_name=? AND status=? ORDER BY version DESC",
            (surface_type, surface_name, status),
        )
    return db.query_all(
        "SELECT * FROM surface_versions "
        "WHERE surface_type=? AND surface_name=? ORDER BY version DESC",
        (surface_type, surface_name),
    )


def get_latest_version(surface_type: str, surface_name: str) -> dict[str, Any] | None:
    """取某条线的最新版本（max version，不论 status）。"""
    rows = list_versions(surface_type, surface_name)
    return rows[0] if rows else None


def get_approved_version(surface_type: str, surface_name: str) -> dict[str, Any] | None:
    """取某条线当前 approved 的最高版本。

    manifest 聚合（D7）用此方法：每个 (type, name) 取其 approved 最高版本。
    若无 approved 版本返回 None（该 surface 在 manifest 中缺失，装配时按缺失处理）。
    """
    rows = list_versions(surface_type, surface_name, status=STATUS_APPROVED)
    return rows[0] if rows else None


def list_by_scope(scope: str, *, status: str | None = None) -> list[dict[str, Any]]:
    """列某 scope（subagent）的所有 surface 版本。

    A/B 实验范围界定（D9）用：改某 surface 只重跑该 scope 的测试项。
    返回的是「版本行」列表，同一条线可能出现多个版本（如多个 approved）。
    取每条线的 approved 最高版用 get_approved_version 逐线查。
    """
    if status:
        return db.query_all(
            "SELECT * FROM surface_versions WHERE scope=? AND status=? ORDER BY version DESC",
            (scope, status),
        )
    return db.query_all(
        "SELECT * FROM surface_versions WHERE scope=? ORDER BY version DESC",
        (scope,),
    )


def list_all_approved_grouped() -> dict[tuple[str, str], dict[str, Any]]:
    """取所有 surface 线各自的 approved 最高版本，按 (type, name) 分组。

    manifest 聚合发布（D7 + D12）的主查询：一次拿到所有该进 manifest 的版本。
    Returns: {(surface_type, surface_name): version_row}。
    """
    rows = db.query_all(
        "SELECT * FROM surface_versions WHERE status=? ORDER BY version DESC",
        (STATUS_APPROVED,),
    )
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["surface_type"], row["surface_name"])
        # 倒序遍历，首次出现的即该线最高 version
        if key not in grouped:
            grouped[key] = row
    return grouped


# ── 状态流转 ─────────────────────────────────────────────────


def update_status(version_id: int, status: str, **extra: Any) -> None:
    """更新版本状态（流水线推进：draft→static_checked→ab_testing→approved）。

    extra 支持更新伴随字段：static_check_passed / proposer_meta。
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"非法 status: {status}。合法值: {sorted(VALID_STATUSES)}")
    ver = db.query_one("SELECT id FROM surface_versions WHERE id=?", (version_id,))
    if ver is None:
        raise ValueError(f"surface 版本不存在: {version_id}")

    sets = ["status = ?"]
    vals: list[Any] = [status]
    if "static_check_passed" in extra:
        scp = extra["static_check_passed"]
        sets.append("static_check_passed = ?")
        vals.append(1 if scp is True else (0 if scp is False else None))
    if "proposer_meta" in extra:
        sets.append("proposer_meta = ?")
        vals.append(json.dumps(extra["proposer_meta"] or {}, ensure_ascii=False))
    vals.append(version_id)
    db.execute(
        f"UPDATE surface_versions SET {', '.join(sets)} WHERE id=?",
        vals,
    )


def approve(version_id: int) -> dict[str, Any]:
    """标记版本 approved（A/B 胜出 + 人工批准后调用）。

    approved 的版本才会被 manifest 聚合（D7）。注意：本方法只标 status，
    不生成 manifest——manifest 发布是独立动作（manifest_publisher，T3.3）。
    """
    update_status(version_id, STATUS_APPROVED)
    return get_version_by_id(version_id)  # type: ignore[return-value]


def reject(version_id: int) -> dict[str, Any]:
    """标记版本 rejected（A/B 失败或人工拒绝）。"""
    update_status(version_id, STATUS_REJECTED)
    return get_version_by_id(version_id)  # type: ignore[return-value]


# ── 便捷读取（供执行端 loader / proposer 用）────────────────


def get_content(surface_type: str, surface_name: str, version: int) -> dict[str, Any] | None:
    """按 (type, name, version) 取版本内容（含 config 解析）。

    执行端 manifest_loader 按 manifest entries 的 version 指针调此方法拉内容。
    """
    row = get_version(surface_type, surface_name, version)
    if row is None:
        return None
    return {
        "surface_type": row["surface_type"],
        "surface_name": row["surface_name"],
        "scope": row["scope"],
        "version": row["version"],
        "content": row["content"],
        "content_kind": row["content_kind"],
        "config": json.loads(row["config"] or "{}"),
        "id": row["id"],
    }
