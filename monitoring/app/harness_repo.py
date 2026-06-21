"""harness 版本管理（Phase 2 T2.4，D2 代码定义 + S8 文件系统+git）。

职责：
  - harness_versions 表 CRUD（版本管理）
  - label 管理：production/latest/candidate，同 label 只指向一个版本（互斥，复用 prompt 模式）
  - harness 代码的文件系统存储 + git 操作
  - 按 label 拉取版本（执行端/worker 用）

harness 版本形态：一个 WriterHarness 实现的代码文件（harnesses/<id>/harness.py）。
proposer 生成候选 = 新 harness.py 文件 + 新版本记录。
批准上线 = candidate 版本升 production label。

设计依据：设计文档 S8 + harness_versions 表 + C5（代码 volume 挂载）。
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import app.db as db

logger = logging.getLogger("monitoring.harness_repo")

PRODUCTION_LABEL = "production"
LATEST_LABEL = "latest"
CANDIDATE_LABEL = "candidate"

# harness 代码存储根目录（默认 backend/harnesses，可被 settings 覆盖）
# 注意：monitoring 和 backend 是两个服务，这里存的是「管理元数据指向的路径」，
# 实际代码文件由 backend 侧写入/读取。monitoring 只记录路径 + 操作 git。


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_labels(labels_str: str | None) -> list[str]:
    return [s.strip() for s in (labels_str or "").split(",") if s.strip()]


def _join_labels(labels: list[str]) -> str:
    return ",".join(labels)


def _strip_label_from_others(keep_version_id: int | None, label: str) -> None:
    """从其它版本移除指定 label（label 互斥）。"""
    rows = db.query_all("SELECT id, labels FROM harness_versions")
    for row in rows:
        if keep_version_id is not None and row["id"] == keep_version_id:
            continue
        current = _parse_labels(row["labels"])
        if label in current:
            current.remove(label)
            db.execute(
                "UPDATE harness_versions SET labels = ? WHERE id = ?",
                (_join_labels(current), row["id"]),
            )


# ── 版本管理 ────────────────────────────────────────────────


def create_version(
    code_path: str,
    *,
    parent_version: int | None = None,
    source: str = "initial",
    labels: list[str] | None = None,
    signature_id: int | None = None,
    proposer_meta: dict[str, Any] | None = None,
    status: str = "draft",
    git_commit: str | None = None,
) -> dict[str, Any]:
    """创建新 harness 版本：version = max+1。

    label 互斥：若指定 labels，先从其它版本移除。
    code_path 是 harness 代码文件的绝对/相对路径。
    """
    latest = db.query_one("SELECT MAX(version) AS mv FROM harness_versions")
    next_version = (latest["mv"] or 0) + 1 if latest and latest["mv"] else 1

    final_labels = list(labels) if labels is not None else [LATEST_LABEL]
    for label in final_labels:
        _strip_label_from_others(None, label)

    db.execute(
        """INSERT INTO harness_versions
           (version, code_path, git_commit, parent_version, source, labels,
            signature_id, proposer_meta, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            next_version, code_path, git_commit, parent_version, source,
            _join_labels(final_labels), signature_id,
            json.dumps(proposer_meta or {}, ensure_ascii=False),
            status, _now(),
        ),
    )
    return get_version(next_version)  # type: ignore[return-value]


def get_version(version: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM harness_versions WHERE version = ?", (version,))


def get_version_by_id(version_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM harness_versions WHERE id = ?", (version_id,))


def get_version_by_label(label: str) -> dict[str, Any] | None:
    """按 label 拉取版本（label 互斥保证唯一）。执行端/worker 主入口。"""
    rows = db.query_all(
        "SELECT * FROM harness_versions ORDER BY version DESC"
    )
    for row in rows:
        if label in _parse_labels(row["labels"]):
            return row
    return None


def list_versions(status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return db.query_all(
            "SELECT * FROM harness_versions WHERE status=? ORDER BY version DESC",
            (status,),
        )
    return db.query_all("SELECT * FROM harness_versions ORDER BY version DESC")


def set_labels(version_id: int, labels: list[str]) -> None:
    """设置版本 labels（label 互斥）。批准上线 = set_labels(id, [production, latest])。"""
    ver = db.query_one("SELECT id FROM harness_versions WHERE id = ?", (version_id,))
    if ver is None:
        raise ValueError(f"harness 版本不存在: {version_id}")
    for label in labels:
        _strip_label_from_others(version_id, label)
    db.execute(
        "UPDATE harness_versions SET labels = ? WHERE id = ?",
        (_join_labels(labels), version_id),
    )


def update_status(version_id: int, status: str, **extra: Any) -> None:
    """更新版本状态（流水线推进：draft→sandbox→static→ab→approved）。"""
    sets = ["status = ?"]
    vals: list[Any] = [status]
    if "git_commit" in extra:
        sets.append("git_commit = ?")
        vals.append(extra["git_commit"])
    if "proposer_meta" in extra:
        sets.append("proposer_meta = ?")
        vals.append(json.dumps(extra["proposer_meta"], ensure_ascii=False))
    vals.append(version_id)
    db.execute(
        f"UPDATE harness_versions SET {', '.join(sets)} WHERE id = ?",
        vals,
    )


def get_production_version() -> dict[str, Any] | None:
    """取当前 production 版本（执行端默认加载的 harness）。"""
    return get_version_by_label(PRODUCTION_LABEL)


def promote_to_production(version_id: int) -> dict[str, Any]:
    """批准上线：版本升 production + latest，原 production 降级。

    流水线最后一环（D17 人工批准后调用）。
    """
    ver = get_version_by_id(version_id)
    if ver is None:
        raise ValueError(f"harness 版本不存在: {version_id}")
    set_labels(version_id, [PRODUCTION_LABEL, LATEST_LABEL])
    update_status(version_id, "approved")
    return get_version_by_id(version_id)  # type: ignore[return-value]


# ── harness 代码存储（文件系统）──────────────────────────────


def write_harness_code(harnesses_root: Path, code: str, version_id: int) -> Path:
    """把 harness 代码写到文件系统。

    harnesses_root/<version_id>/harness.py
    返回写入的路径。
    """
    version_dir = Path(harnesses_root) / str(version_id)
    version_dir.mkdir(parents=True, exist_ok=True)
    code_path = version_dir / "harness.py"
    code_path.write_text(code, encoding="utf-8")
    return code_path


def read_harness_code(code_path: str | Path) -> str:
    """读 harness 代码文件内容。"""
    return Path(code_path).read_text(encoding="utf-8")


def get_harness_diff(version_a: int, version_b: int, harnesses_root: Path) -> str:
    """生成两个版本代码的 diff（proposer 改了什么）。"""
    import difflib
    va = get_version(version_a)
    vb = get_version(version_b)
    if va is None or vb is None:
        return ""
    code_a = read_harness_code(va["code_path"]).splitlines(keepends=True)
    code_b = read_harness_code(vb["code_path"]).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            code_a, code_b,
            fromfile=f"v{version_a}/harness.py",
            tofile=f"v{version_b}/harness.py",
        )
    )
