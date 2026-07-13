"""harness 版本 reconcile（去 DB 重构：executor 自愈机制）。

防止 evolution 发布后 push 通知丢失导致 executor 永久停在旧版本。
后台周期性（10分钟）比较本地 production checkout 的 commit 和 bare repo main 的 commit，
不同就 reload（git pull + 重新加载包）。

这是 GitOps 第4原则「Continuously Reconciled」的轻量实现：
  - 主路径：evolution ship → notify_executor → /reload（即时）
  - 兜底路径：本模块周期 reconcile（10分钟内自愈）

参照 trace_recorder.start_drain 的后台协程模式（幂等 task + aclose）。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.platform.agent.git_sync import production_commit, pull_production
from app.platform.agent.loader import reload_current

logger = logging.getLogger("writer.harness_reconcile")

_RECONCILE_INTERVAL = 600  # 10 分钟（秒）
_reconcile_task: asyncio.Task | None = None


def _bare_repo_main_commit() -> str | None:
    """bare repo 的 main 分支当前 commit（executor 应该跑的版本）。

    通过 git ls-remote 或直接查 bare repo 的 refs 取 main HEAD。
    返回空串表示 bare repo 未就绪（首次启动可能还没 push）。
    """
    import subprocess
    from app.platform.core.settings import get_settings

    bare = get_settings().harness_bare_repo
    # 相对路径基于项目根解析（和 git_sync._project_root 一致：parents[4] = Writer/）
    from pathlib import Path
    if not Path(bare).is_absolute():
        bare = str(Path(__file__).resolve().parents[4] / bare)

    try:
        result = subprocess.run(
            ["git", "-C", bare, "rev-parse", "--short", "main"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        logger.debug("查 bare repo main commit 失败", exc_info=True)
    return ""


def _reconcile_once() -> bool:
    """执行一次 reconcile：比较 commit，不同则 reload。

    Returns: True=触发了 reload，False=无需 reload（已同步或 bare 未就绪）。
    """
    target = _bare_repo_main_commit()
    if not target:
        # bare repo 无 main（首次启动 evolution 还没 push），跳过
        logger.debug("bare repo 无 main，跳过 reconcile")
        return False

    current = production_commit()
    if current == target:
        return False  # 已同步

    logger.info("reconcile 发现版本漂移：本地 %s → bare main %s，触发 reload", current, target)
    try:
        pull_production()  # git reset --hard origin/main
        reload_current()   # 清缓存 + 重新加载包
        logger.info("reconcile reload 完成")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("reconcile reload 失败（下次重试）")
        return False


async def _reconcile_loop() -> None:
    """后台 reconcile 循环：周期检查 + 变了就 reload。"""
    while True:
        await asyncio.sleep(_RECONCILE_INTERVAL)
        try:
            await asyncio.to_thread(_reconcile_once)
        except Exception:  # noqa: BLE001
            # reconcile 异常不应拖垮后台任务，静默继续下一轮
            logger.debug("reconcile 异常（继续下一轮）", exc_info=True)


def start_reconcile() -> None:
    """启动 reconcile 后台协程（由应用 lifespan 调用，幂等）。

    必须在事件循环线程内调用（asyncio.create_task 依赖运行中的 loop）。
    """
    global _reconcile_task
    if _reconcile_task is None or _reconcile_task.done():
        _reconcile_task = asyncio.create_task(_reconcile_loop())
        logger.info("harness reconcile 已启动（间隔 %ds）", _RECONCILE_INTERVAL)


async def aclose_reconcile() -> None:
    """关闭 reconcile 协程（由 lifespan shutdown 调用）。"""
    global _reconcile_task
    if _reconcile_task is not None:
        _reconcile_task.cancel()
        with suppress(asyncio.CancelledError):
            await _reconcile_task
        _reconcile_task = None
