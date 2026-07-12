"""活跃 trace 轮询（Phase 6 T15）。

定期轮询执行端 /internal/active-runs，缓存到内存（不存 DB，D22 进行中不入库）。
页面读内存缓存展示活跃大盘。

设计依据：T15（轮询拉取，只展示不存储）+ D21（只看活跃大盘）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from app.core.settings import settings

logger = logging.getLogger("evolution.active")

router = APIRouter(tags=["active"])

# 执行端 internal 接点（拉活跃 trace）。
# 执行端地址优先用 evolution 配置里的 executor_url，否则默认 localhost:8000。
_POLL_INTERVAL = 5.0  # 轮询间隔（秒）

# 内存缓存（进程级，重启丢失——无妨，活跃大盘是实时观测，不需持久）。
_active_cache: list[dict[str, Any]] = []
_task: asyncio.Task | None = None


def start_active_poller() -> None:
    """启动轮询后台任务（幂等）。在 lifespan 启动时调用。"""
    global _task
    executor_url = getattr(settings, "executor_url", "") or "http://localhost:8000"
    if _task is None or _task.done():
        _task = asyncio.create_task(_poll_loop(executor_url))


def get_active_runs() -> list[dict[str, Any]]:
    """读取缓存的活跃 trace列表（页面用）。"""
    return list(_active_cache)


async def _poll_loop(executor_url: str) -> None:
    """周期轮询执行端活跃 trace。失败静默（执行端不可用不影响 evolution）。"""
    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            await asyncio.to_thread(_poll_once, executor_url)
        except Exception:
            logger.debug("活跃 trace 轮询失败", exc_info=True)


def _poll_once(executor_url: str) -> None:
    """轮询一次执行端 /internal/active-runs。"""
    global _active_cache
    try:
        import httpx

        url = f"{executor_url.rstrip('/')}/internal/active-runs"
        resp = httpx.get(url, timeout=3.0)
        resp.raise_for_status()
        _active_cache = resp.json()
    except Exception:
        # 执行端不可用 → 清空缓存（活跃大盘显示空，不报错）
        _active_cache = []


# ── D7：富化 JSON 端点（供监测前端轮询）──


@router.get("/active-runs")
def active_runs_api() -> list[dict[str, Any]]:
    """活跃 trace 富化列表（D7 + D9）。

    数据源合并：
    - executor 活跃 trace（轮询缓存）：创作端正在跑的 trace
    - evolution recorder 活跃 trace（D9 新增）：进化端正在跑的评估/进化 trace

    join evolution.db runs 表补 session_name + run_purpose + ingested 标记。
    未摄入的活跃 trace join 不到 → session_name/run_purpose=null 降级，ingested=false。
    """
    # ── 合并两个数据源 ──
    runs = get_active_runs()  # executor 活跃 trace（轮询缓存）

    # D9：合并 evolution recorder 自己的活跃 trace
    evo_runs = _get_evolution_active_runs()
    all_trace_ids = {r.get("trace_id", "") for r in runs if r.get("trace_id")}
    for er in evo_runs:
        if er.get("trace_id") and er["trace_id"] not in all_trace_ids:
            runs.append(er)
            all_trace_ids.add(er["trace_id"])

    if not runs:
        return []

    # 批量查 evolution.db，一次拿全部活跃 trace_id 的 session_name + run_purpose
    import app.core.db as db

    trace_ids = [r.get("trace_id", "") for r in runs if r.get("trace_id")]
    enriched: list[dict[str, Any]] = []
    if trace_ids:
        placeholders = ",".join("?" * len(trace_ids))
        rows = db.query_all(
            f"SELECT trace_id, session_name, run_purpose FROM runs WHERE trace_id IN ({placeholders})",
            tuple(trace_ids),
        )
    else:
        rows = []
    ingested_map = {
        r["trace_id"]: {
            "session_name": r.get("session_name"),
            "run_purpose": r.get("run_purpose"),
        }
        for r in rows
    }

    for r in runs:
        tid = r.get("trace_id", "")
        meta = ingested_map.get(tid, {})
        enriched.append({
            "trace_id": tid,
            "workspace_id": r.get("workspace_id", ""),
            "thread_id": r.get("thread_id"),
            "endpoint": r.get("endpoint"),
            "status": r.get("status", "running"),
            "started_at": r.get("started_at"),
            "duration_ms": r.get("duration_ms"),
            "event_count": r.get("event_count", 0),
            # D7 富化：join 不到时 null（前端降级显示 workspace_id/endpoint）
            "session_name": meta.get("session_name"),
            "ingested": tid in ingested_map,
            # D9：run_purpose（executor trace 优先用 r 自带的，join 不到时降级）
            # evolution recorder 的活跃 trace 已自带 run_purpose（recorder.list_active_runs 返回）
            "run_purpose": r.get("run_purpose") or meta.get("run_purpose") or "user_generation",
        })
    return enriched


def _get_evolution_active_runs() -> list[dict[str, Any]]:
    """获取 evolution recorder 自己的活跃 trace（D9）。

    evolution 端的评估/进化 agent 运行时，trace 由 EvolutionTraceRecorder 记录，
    不经过 executor 的 active-runs 轮询。这里从 app.state.trace_recorder 取内存中活跃列表。
    """
    try:
        from app.main import app
        recorder = getattr(app.state, "trace_recorder", None)
        if recorder is None:
            return []
        return recorder.list_active_runs()
    except Exception:
        return []
