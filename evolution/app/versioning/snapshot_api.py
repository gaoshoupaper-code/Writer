"""snapshot API（去 DB 重构：数据源从 harness_snapshots 表 → registry.json）。

提供整包级的版本查询 API。数据源是 harness 独立仓库内的 registry.json
（版本注册表：版本列表 / 谱系 / production 指针）。

端点（/api/snapshots 前缀）：
  GET  /snapshots                 列版本（按版本倒序，含 status）
  GET  /snapshots/production      当前 production 版本
  GET  /snapshots/{version}       指定版本元数据

版本内容（源码文件）不在本端点返回——通过 /snapshots/{version}/elements 取
（elements_api 从 git 读取真实源文件）。

设计依据：设计文档 20260713_003000（去 DB 轻量化重构）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import db
from app.versioning import registry_repo
from app.trace.facts import append_release_event

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


class RollbackRequest(BaseModel):
    to_version: int
    reason: str = ""


def _compensate_failed_rollback(
    current: dict[str, Any], failed_target_version: int
) -> str | None:
    """恢复 registry 与 executor；返回 None 表示补偿已确认。"""
    from app.core import git_ops
    from app.versioning.snapshot_publisher import reload_executor

    try:
        registry_repo.restore_failed_rollback(current["version"], failed_target_version)
        git_ops.commit_registry_and_push(
            f"恢复 Harness production v{current['version']}: rollback 激活失败"
        )
        restored = reload_executor(current["version"])
        if restored.get("commit") != current.get("commit_hash"):
            raise RuntimeError("executor 未恢复到原 production commit")
        expected_identity = current.get("runtime_identity") or {}
        actual_identity = restored.get("runtime_identity") or {}
        if expected_identity and (
            actual_identity.get("identity_digest")
            != expected_identity.get("identity_digest")
        ):
            raise RuntimeError("executor 未恢复到原 production runtime identity")
        return None
    except Exception as exc:
        return str(exc)


@router.get("")
def list_snapshots(status: str | None = None) -> list[dict[str, Any]]:
    """列版本（按版本倒序）。可按 status 过滤（production/retired）。"""
    versions = registry_repo.list_versions()
    if status:
        versions = [v for v in versions if v["status"] == status]
    return versions


@router.get("/production")
def get_production_snapshot() -> dict[str, Any]:
    """当前 production 版本（元数据）。无则 404。"""
    snap = registry_repo.get_production_version()
    if snap is None:
        raise HTTPException(status_code=404, detail="无 production 版本")
    return snap


@router.post("/rollback")
def rollback_snapshot(body: RollbackRequest, request: Request) -> dict[str, Any]:
    """移动 production 指针并 reload executor；确认后记录 rollback_activated。"""
    current = registry_repo.get_production_version()
    if current is None:
        raise HTTPException(status_code=409, detail="无 production 版本可回滚")
    if current["version"] == body.to_version:
        raise HTTPException(status_code=409, detail="目标版本已是 production")

    source_candidate_id = f"harness-version-{current['version']}"
    release = db.query_one(
        """SELECT release_id FROM release_events_v2
           WHERE candidate_id=? AND status IN ('activated', 'activation_failed')
           ORDER BY rowid DESC LIMIT 1""",
        (source_candidate_id,),
    )

    from app.core import git_ops
    from app.versioning.snapshot_publisher import reload_executor

    try:
        target = registry_repo.rollback(body.to_version, reason=body.reason or None)
        try:
            registry_commit = git_ops.commit_registry_and_push(
                f"回滚 production v{current['version']} -> v{body.to_version}"
            )
        except Exception as exc:
            restore_error = _compensate_failed_rollback(current, body.to_version)
            raise HTTPException(
                status_code=502,
                detail={
                    "message": f"rollback registry 提交失败：{exc}",
                    "executor_restore_error": restore_error,
                },
            ) from exc
        try:
            activated = reload_executor(body.to_version)
            activated_identity = activated.get("runtime_identity") or {}
            expected_identity = target.get("runtime_identity") or {}
            if activated.get("commit") != target.get("commit_hash"):
                raise RuntimeError("executor 未加载目标 production commit")
            if expected_identity and (
                activated_identity.get("identity_digest")
                != expected_identity.get("identity_digest")
            ):
                raise RuntimeError("executor runtime identity 与目标版本不一致")
        except Exception as exc:
            restore_error = _compensate_failed_rollback(current, body.to_version)
            raise HTTPException(
                status_code=502,
                detail={
                    "message": f"rollback 激活失败，已恢复原 production：{exc}",
                    "executor_restore_error": restore_error,
                },
            ) from exc
        if release:
            append_release_event(
                release_id=release["release_id"],
                status="rollback_activated",
                candidate_id=source_candidate_id,
                actor_user_id=getattr(request.state, "user_id", None),
            )
        return {
            "status": "rollback_activated",
            "from_version": current["version"],
            "to_version": target["version"],
            "source_commit": target["commit_hash"],
            "registry_commit": registry_commit,
            "release_id": release["release_id"] if release else None,
            "release_tracking": "v2" if release else "legacy",
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"回滚失败：{exc}") from exc


@router.get("/{version}")
def get_snapshot(version: int) -> dict[str, Any]:
    """指定版本元数据。不存在则 404。"""
    snap = registry_repo.get_version(version)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
    return snap
