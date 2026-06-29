"""executor 轮询公共函数（决策 D6）。

把 tests/api.py 的 _poll_task_status 和 evolve/tools.py 的 _run_on_executor
的公共轮询内核提取出来，消除两处重复的 /ab/status 轮询逻辑。

设计要点：
  - poll_executor_task(task_id, is_cancelled) 是纯轮询函数，返回 trace_id。
  - 不耦合任何 DB（test_repo / ev_db 的回填由调用方自己做，或用回调）。
  - 复用 tests/api.py 的健壮处理（404 容忍、cancelled 检测）。
  - evolve/tools.py 的 _run_on_executor 后续改用本函数（Phase 4 驱动器重构时）。

设计依据：设计文档 D6（提取公共轮询函数复用）。
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from app.core.settings import settings

logger = logging.getLogger("evolution.evolve.runner")

# 轮询参数（与 tests/api.py、tools.py 对齐）
POLL_INTERVAL = 3.0           # 轮询间隔（秒）
POLL_TIMEOUT = 600.0          # 单次生成最长等待（10 分钟，一次完整写作管线）
CONNECT_TIMEOUT = 10.0        # 单次 HTTP 请求超时
MAX_NOT_FOUND = 5             # 连续 404 容忍次数（防进程重启误判）


def executor_url(path: str) -> str:
    """拼接 executor 内部接口 URL。"""
    return f"{settings.executor_url.rstrip('/')}{path}"


def start_ab_run(*, config: dict, demand_md: str, baseline: bool) -> str:
    """调 executor /internal/ab/run 启动一次生成，返回 task_id。

    Args:
        config:    HarnessConfig（baseline 用原始，candidate 用改后的）
        demand_md: 预置 demand.md 内容
        baseline:  True=baseline 模式，False=candidate 模式
    """
    resp = httpx.post(
        executor_url("/internal/ab/run"),
        json={"config": config, "demand_md": demand_md, "baseline": baseline},
        timeout=30.0,
    )
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    logger.info("executor /ab/run 启动: task=%s baseline=%s", task_id, baseline)
    return task_id


def poll_executor_task(
    task_id: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    poll_interval: float = POLL_INTERVAL,
    poll_timeout: float = POLL_TIMEOUT,
) -> str:
    """同步轮询 executor /ab/status 直到完成，返回 trace_id。

    Args:
        task_id:      start_ab_run 返回的 task_id
        is_cancelled: 可选回调，返回 True 时立即中止轮询（抛 CancelledError）
        poll_interval: 轮询间隔（秒）
        poll_timeout:  最长等待（秒），超时抛 TimeoutError

    Returns:
        trace_id

    Raises:
        TimeoutError:        轮询超时
        RuntimeError:        task failed
        asyncio.CancelledError: is_cancelled 回调返回 True
    """
    deadline = time.time() + poll_timeout
    not_found_count = 0

    while time.time() < deadline:
        # 取消检测（用户主动停止）
        if is_cancelled is not None and is_cancelled():
            logger.info("task %s 被取消，中止轮询", task_id)
            import asyncio
            raise asyncio.CancelledError(f"task {task_id} 已取消")

        time.sleep(poll_interval)
        try:
            resp = httpx.get(
                executor_url(f"/internal/ab/status/{task_id}"),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception:
            # 网络抖动，跳过本轮
            continue

        if resp.status_code == 404:
            not_found_count += 1
            if not_found_count >= MAX_NOT_FOUND:
                raise RuntimeError(
                    f"executor task {task_id} 不可达（可能进程重启，连续 {not_found_count} 次 404）"
                )
            continue

        not_found_count = 0
        if resp.status_code != 200:
            continue

        data = resp.json()
        trace_ids = data.get("trace_ids", [])

        if data["status"] == "done":
            if not trace_ids:
                raise RuntimeError(f"executor task {task_id} 完成但无 trace_id")
            logger.info("task %s done: trace=%s", task_id, trace_ids[0])
            return trace_ids[0]

        if data["status"] == "failed":
            raise RuntimeError(f"executor task {task_id} failed: {data.get('error')}")

    raise TimeoutError(f"executor task {task_id} 轮询超时（{poll_timeout}s）")


def run_generation(
    *,
    config: dict,
    demand_md: str,
    baseline: bool,
    is_cancelled: Callable[[], bool] | None = None,
) -> str:
    """一站式：启动生成 + 轮询拿 trace_id（evolve 流水线用）。

    封装 start_ab_run + poll_executor_task，返回 trace_id。
    tests/api.py 的后台线程模式不直接用这个（它需要边轮询边回填 test_repo），
    但可拆开用 start_ab_run + poll_executor_task。
    """
    task_id = start_ab_run(config=config, demand_md=demand_md, baseline=baseline)
    return poll_executor_task(task_id, is_cancelled=is_cancelled)


__all__ = [
    "POLL_INTERVAL",
    "POLL_TIMEOUT",
    "executor_url",
    "start_ab_run",
    "poll_executor_task",
    "run_generation",
]
