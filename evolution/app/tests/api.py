"""手动测试 API —— 发起/列表/重试/Agent 版本选择。

端点：
  POST /api/tests                  发起一次手动测试（选数据集 + 选 Agent 版本）
  GET  /api/tests                  测试记录列表（状态 tab + 分页）
  GET  /api/tests/{test_id}        单条测试记录详情
  POST /api/tests/{test_id}/retry  重试失败的测试（起新记录，retry_of 指向原记录）
  GET  /api/tests/agents           可选 Agent 版本列表（working + 快照）

设计文档：.claude/md/20260628_144027_进化端手动测试入口_设计.md
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core import db
from app.core.settings import settings
from app.common import evalset
from app.versioning.registry_repo import list_versions as list_snapshots
from app.tests import repo as test_repo

logger = logging.getLogger("evolution.tests.api")

router = APIRouter(prefix="/tests", tags=["tests"])

# executor /internal/ab/run 轮询配置（与 evolve/tools.py 对齐）
_EXEC_TIMEOUT = 30.0
_POLL_TIMEOUT = 600.0  # 10 分钟（一次完整写作管线）
_POLL_INTERVAL = 3.0


def _executor_url(path: str) -> str:
    return f"{settings.executor_url.rstrip('/')}{path}"


def _poll_task_status(test_id: str, task_id: str) -> None:
    """后台轮询 executor /ab/status 直到 done/failed/cancelled，回填 trace_id + 驱动终态。

    与 ingest 通知链路（_sync_manual_test_status）互为兜底：先到者更新，后到者
    因 status 已终结而跳过（mark_done/mark_failed/mark_cancelled 检查 status）。
    """
    deadline = time.time() + _POLL_TIMEOUT
    not_found_count = 0
    try:
        while time.time() < deadline:
            time.sleep(_POLL_INTERVAL)
            # 测试已被停止（stop 端点标了 cancelled）→ 退出轮询，不再驱动终态
            row = test_repo.get_test(test_id)
            if row and row["status"] in ("done", "failed", "cancelled"):
                return
            try:
                resp = httpx.get(_executor_url(f"/internal/ab/status/{task_id}"), timeout=10.0)
            except Exception:
                continue
            if resp.status_code == 404:
                # executor 重启会导致内存 task 表丢失。但若 trace_id 已回填，
                # trace 仍可能存活（事件已落盘），交给 ingest 链路判断终态，不判失败。
                not_found_count += 1
                if row and row["trace_id"]:
                    logger.info(
                        "task %s 返回 404 但已有 trace_id=%s，交给 ingest 链路",
                        task_id, row["trace_id"],
                    )
                    return  # 轮询退出，终态由 ingest 驱动
                # trace_id 仍空：容忍几次 404（网络抖动），连续多次才判失败
                if not_found_count >= 5:
                    test_repo.mark_failed(
                        test_id, f"executor task {task_id} 不可达（可能进程重启）"
                    )
                    return
                continue
            not_found_count = 0
            if resp.status_code != 200:
                continue
            data = resp.json()
            # running 期间也可能已有 trace_id（executor create_run 后立即回填）
            trace_ids = data.get("trace_ids", [])
            if trace_ids and data["status"] == "running":
                test_repo.set_trace_id(test_id, trace_ids[0])
            if data["status"] == "done":
                if trace_ids:
                    test_repo.set_trace_id(test_id, trace_ids[0])
                # done 的终态以 ingest 链路为准（trace 摄入后 _sync_manual_test_status 标 done）；
                # 但若 ingest 通知丢失，这里兜底标 done。
                if row and row["status"] not in ("done", "failed", "cancelled"):
                    test_repo.mark_done(test_id, trace_ids[0] if trace_ids else "")
                return
            if data["status"] == "failed":
                # trace 可能已创建（assemble 前的 create_run）但后续抛异常；回填 trace_id
                # 让用户能从失败记录点进去看部分 trace。
                failed_trace_ids = data.get("trace_ids") or []
                test_repo.mark_failed(
                    test_id,
                    data.get("error") or "executor task failed",
                    failed_trace_ids[0] if failed_trace_ids else None,
                )
                return
            if data["status"] == "cancelled":
                # executor 在边界处停了（run_ab_generation 已 cancel_run 收尾）
                if trace_ids:
                    test_repo.set_trace_id(test_id, trace_ids[0])
                if row and row["status"] not in ("done", "failed", "cancelled"):
                    test_repo.mark_cancelled(test_id, trace_ids[0] if trace_ids else None)
                return
        # 超时
        if row and row["status"] not in ("done", "failed", "cancelled"):
            test_repo.mark_failed(test_id, "轮询超时（10 分钟无结果）")
    except Exception as exc:
        logger.exception("轮询测试 %s 状态失败", test_id)
        try:
            test_repo.mark_failed(test_id, f"轮询异常: {exc}")
        except Exception:
            pass


# ── 请求/响应模型 ──────────────────────────────────────────────


class StartTestRequest(BaseModel):
    case_id: str
    version_type: Literal["working", "snapshot"]
    version_id: int | None = None  # snapshot 时必填


class StartTestResponse(BaseModel):
    test_id: str
    status: str  # pending


# ── Agent 版本列表 ────────────────────────────────────────────


def _working_commit() -> str:
    """读 harnesses/current 的 git HEAD 短 hash（失败返回空串）。"""
    pkg = Path(settings._evolution_root) / "harnesses" / "current"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=pkg, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


@router.get("/agents")
def list_agents() -> dict[str, Any]:
    """可选 Agent 版本列表（working + 快照，决策 D-Q11）。

    working 排首位，快照按 version 降序。
    """
    agents: list[dict[str, Any]] = [
        {
            "type": "working",
            "label": "working（未固化）",
            "source_commit": _working_commit(),
        }
    ]
    from app.versioning.registry_repo import get_version_commit
    for snap in list_snapshots():
        agents.append(
            {
                "type": "snapshot",
                "version": snap["version"],
                "label": f"v{snap['version']}",
                "source_commit": get_version_commit(snap["version"]) or "",
                "change_summary": snap.get("change_summary") or "",
            }
        )
    return {"agents": agents}


# ── 发起测试 ──────────────────────────────────────────────────


def _validate_version(version_type: str, version_id: int | None) -> dict[str, Any] | None:
    """校验版本选择，返回版本元数据（snapshot 时）或 None（working 时）。无效 raise 400。"""
    if version_type == "working":
        return None
    if version_id is None:
        raise HTTPException(status_code=400, detail="snapshot 版本必须指定 version_id")
    from app.versioning.registry_repo import get_version, get_version_commit

    snap = get_version(version_id)
    if snap is None:
        raise HTTPException(status_code=400, detail=f"version not found: {version_id}")
    commit = get_version_commit(version_id)
    if not commit:
        raise HTTPException(
            status_code=400,
            detail=f"snapshot v{version_id} 无对应 commit（迁移历史版本，不可执行）",
        )
    snap["source_commit"] = commit
    return snap


def _trigger_executor(
    *, demand_md: str, version_type: str, snapshot: dict[str, Any] | None
) -> str:
    """调 executor /internal/ab/run，返回 task_id。

    working: baseline=true + source_commit=None（executor 用 harnesses/current 硬编码装配）
    snapshot: baseline=false + source_commit（executor 按 commit checkout 源码装配，
              config=None 用源码包内硬编码 assemble）

    去 DB 重构后版本 = git commit，"配置"即源码本身，不再需要序列化的 config_json
    （与 benchmark/runner.py._trigger_executor 对齐）。
    """
    if version_type == "working":
        source_commit = None
    else:
        # _validate_version 已校验过 commit 非空并塞进 snapshot["source_commit"]
        source_commit = snapshot["source_commit"] if snapshot else None

    payload: dict[str, Any] = {
        "config": None,
        "demand_md": demand_md,
        "baseline": version_type == "working",
        "source_commit": source_commit,
    }

    resp = httpx.post(_executor_url("/internal/ab/run"), json=payload, timeout=_EXEC_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["task_id"]


def _start_test_internal(
    *, case_id: str, version_type: str, version_id: int | None, retry_of: str | None
) -> str:
    """发起测试的共享逻辑（新建 + 发起），返回 test_id。"""
    # 校验 case
    if not evalset.case_exists(case_id):
        raise HTTPException(status_code=400, detail=f"case not found: {case_id}")
    # 校验版本
    snapshot = _validate_version(version_type, version_id)

    # 推导 origin_layer（决策 A6）：从 evalset 查 case 所在层，进化 Agent 据此区分验证/探索
    origin_layer = evalset.resolve_layer(case_id)

    # 创建 pending 记录
    test_id = test_repo.create_test(
        case_id=case_id,
        version_type=version_type,
        version_id=version_id,
        retry_of=retry_of,
        origin_layer=origin_layer,
    )

    # 读 demand_md + 调 executor
    try:
        demand_md = evalset.load_case_demand(case_id)
        task_id = _trigger_executor(
            demand_md=demand_md, version_type=version_type, snapshot=snapshot
        )
        test_repo.mark_running(test_id, task_id)
        # 后台轮询 executor task 状态，回填 trace_id + 驱动终态（与 ingest 链路互为兜底）
        threading.Thread(
            target=_poll_task_status, args=(test_id, task_id), daemon=True
        ).start()
        logger.info(
            "测试 %s 已发起: case=%s version=%s%s task=%s",
            test_id, case_id, version_type,
            f" v{version_id}" if version_id else "", task_id,
        )
    except httpx.HTTPError as exc:
        test_repo.mark_failed(test_id, f"executor 调用失败: {exc}")
        raise HTTPException(status_code=502, detail=f"executor 调用失败: {exc}")
    except Exception as exc:
        test_repo.mark_failed(test_id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    return test_id


@router.post("", response_model=StartTestResponse, status_code=202)
def start_test(req: StartTestRequest) -> StartTestResponse:
    """发起一次手动测试。"""
    test_id = _start_test_internal(
        case_id=req.case_id,
        version_type=req.version_type,
        version_id=req.version_id,
        retry_of=None,
    )
    return StartTestResponse(test_id=test_id, status="pending")


# ── 列表 / 详情 / 重试 ────────────────────────────────────────


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "test_id": row["test_id"],
        "case_id": row["case_id"],
        "version_type": row["version_type"],
        "version_id": row["version_id"],
        "trace_id": row["trace_id"],
        "task_id": row["task_id"],
        "status": row["status"],
        "error": row["error"],
        "retry_of": row["retry_of"],
        "origin_layer": row.get("origin_layer"),
        "created_at": row["created_at"],
    }


@router.get("")
def list_tests(
    status: str = Query("全部", description="全部/pending/running/done/failed"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """测试记录列表（状态过滤 + created_at 倒序分页）。"""
    rows, total = test_repo.list_tests(status=status, page=page, page_size=page_size)
    return {
        "tests": [_row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{test_id}")
def get_test(test_id: str) -> dict[str, Any]:
    """单条测试记录详情。"""
    row = test_repo.get_test(test_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"test not found: {test_id}")
    return _row_to_dict(row)


@router.post("/{test_id}/retry", response_model=StartTestResponse, status_code=202)
def retry_test(test_id: str) -> StartTestResponse:
    """重试失败的测试（起新记录，retry_of 指向原记录，决策 D11）。"""
    row = test_repo.get_test(test_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"test not found: {test_id}")
    if row["status"] != "failed":
        raise HTTPException(status_code=400, detail="only failed tests can be retried")

    new_id = _start_test_internal(
        case_id=row["case_id"],
        version_type=row["version_type"],
        version_id=row["version_id"],
        retry_of=test_id,
    )
    return StartTestResponse(test_id=new_id, status="pending")


# ── 停止 / 删除 ────────────────────────────────────────────────


@router.post("/{test_id}/stop")
def stop_test(test_id: str) -> dict[str, Any]:
    """停止运行中的测试（super-step 边界停，非立即）。

    流程：
      1. 校验记录存在且 status ∈ {pending, running}（终态不可停）
      2. 调 executor POST /internal/ab/stop/{task_id} 设取消标志（失败仅记日志，
         不阻塞——executor 不可达时本地仍标 cancelled，executor 侧任务会自然结束
         或被轮询超时兜底）
      3. mark_cancelled 本地标记

    真正中断发生在 executor 的 run_ab_generation 下一个 super-step 边界（数秒延迟），
    轮询线程随后会确认 cancelled 终态。已生成的部分内容保留在 trace 中。
    """
    row = test_repo.get_test(test_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"test not found: {test_id}")
    if row["status"] not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"测试已终态（{row['status']}），无需停止",
        )

    task_id = row.get("task_id")
    if task_id:
        try:
            resp = httpx.post(
                _executor_url(f"/internal/ab/stop/{task_id}"), timeout=10.0
            )
            if resp.status_code == 409:
                # executor 认为已终态——可能竞态（任务刚好结束）。本地以实际为准。
                logger.info(
                    "executor task %s 已终态（409），本地仍标记 cancelled", task_id
                )
            elif resp.status_code != 200:
                logger.warning(
                    "executor stop 返回 %s: %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            # executor 不可达不阻塞：本地仍标 cancelled，executor 任务会被轮询超时兜底
            logger.warning("调用 executor stop 失败（本地仍标记 cancelled）: %s", exc)

    test_repo.mark_cancelled(test_id, row.get("trace_id"))
    logger.info("测试 %s 已标记 cancelled", test_id)
    return {"status": "cancelled", "test_id": test_id}


@router.delete("/{test_id}")
def delete_test(test_id: str) -> dict[str, Any]:
    """删除测试记录 + 关联 trace 数据（仅终态可删）。

    删除范围：
      - manual_tests 记录行（本表）
      - evolution 侧 trace 副本：runs / nodes / event_payloads 三表
        （DELETE FROM runs，靠外键 ON DELETE CASCADE 级联清 nodes/events）

    executor 端的 jsonl 文件保留（AB 临时 workspace 产物，跨服务删除需维护 thread
    信息且不在用户可见范围）。trace 链接删除后失效。

    running/pending 的测试必须先停止（前端会引导）。
    """
    row = test_repo.get_test(test_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"test not found: {test_id}")
    if row["status"] in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail="请先停止运行中的测试再删除",
        )

    trace_id = row.get("trace_id")
    deleted_trace = False
    if trace_id:
        # 删 runs 行，nodes/event_payloads 靠 ON DELETE CASCADE 清除（同 traces.py:113 模式）
        cur = db.execute("DELETE FROM runs WHERE trace_id=?", (trace_id,))
        deleted_trace = cur.rowcount > 0

    test_repo.delete_test(test_id)
    logger.info(
        "测试记录 %s 已删除（trace_id=%s, trace副本删除=%s）",
        test_id, trace_id, deleted_trace,
    )
    return {
        "status": "ok",
        "deleted": test_id,
        "trace_id": trace_id,
        "trace_removed": deleted_trace,
    }


__all__ = ["router"]
