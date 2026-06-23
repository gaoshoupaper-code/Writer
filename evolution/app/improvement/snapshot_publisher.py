"""snapshot 发布器（Phase 7 T5.3，取代 manifest_publisher）。

封装「包文件变更 → 发布新快照 → 通知执行端」完整流程。

职责边界：
  - publish_and_notify：发布当前包快照 + 通知执行端（evolution 改完包文件后调）
  - notify_executor：HTTP 通知执行端"有新 production 快照"（执行端 mark stale）

与 manifest_publisher 的区别（Phase 6 → Phase 7）：
  - manifest_publisher：approve surface_version → 聚合 manifest → 通知（surface 级）
  - snapshot_publisher：包文件已改 → tar 整包发快照 → 通知（整包级）

通知机制（沿用 D5 降级模式）：
  evolution 发布新快照 → POST executor /internal/snapshot/refreshed
  → 执行端标记 stale → 下次 load_current_package 重载（进程级缓存）
  通知彻底降级：executor_url 未配置/失败 → 静默吞掉。

设计依据：设计文档 D6=①（整包快照）+ T5.3 + D5（通知降级）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.settings import settings
from app.improvement import snapshot_repo

logger = logging.getLogger("evolution.snapshot_publisher")


def publish_and_notify(
    package_dir: Path | None = None,
    *,
    change_summary: str | None = None,
) -> dict[str, Any] | None:
    """发布当前包快照并通知执行端。

    场景：evolution 改完包文件（手动/propose 后）→ 调本函数固化为新版本快照。

    Args:
        package_dir: 包目录。None = 默认 evolution/harnesses/current/。
        change_summary: 本版改了哪些文件。None = 从 manifest.json 读。

    Returns: {snapshot_version, notified} 或 None（发布失败）。
    """
    if package_dir is None:
        # harnesses/ 在 evolution/ 根下（非 app/）。settings 模块文件在 app/core/，
        # 往上三级到 evolution/。
        import app.core.settings as _settings_mod
        evolution_root = Path(_settings_mod.__file__).resolve().parents[2]
        package_dir = evolution_root / "harnesses" / "current"

    snapshot = snapshot_repo.publish_production(
        package_dir, change_summary=change_summary,
    )
    if snapshot is None:
        logger.error("发布快照失败（包目录无 manifest.json？）")
        return None

    notified = notify_executor(snapshot["version"])
    return {
        "snapshot_version": snapshot["version"],
        "notified": notified,
    }


def notify_executor(snapshot_version: int) -> bool:
    """通知执行端「新 production 快照发布了」。

    彻底降级：
      - executor_url 未配置 → 不通知（返回 False，但不报错）
      - 网络失败/超时 → 静默吞掉（返回 False）
    执行端下次 load_current_package 会重载（进程级缓存，D11 换版本重启语义）。

    Returns: True=通知成功，False=未配置或失败。
    """
    executor_url = getattr(settings, "executor_url", "").rstrip("/")
    if not executor_url:
        logger.debug("executor_url 未配置，跳过快照通知")
        return False
    try:
        import httpx
        resp = httpx.post(
            f"{executor_url}/internal/snapshot/refreshed",
            json={"snapshot_version": snapshot_version},
            timeout=2.0,
        )
        if resp.status_code < 300:
            logger.info("已通知执行端快照 v%s", snapshot_version)
            return True
        logger.warning("通知执行端快照失败: HTTP %s", resp.status_code)
        return False
    except Exception:
        logger.debug("通知执行端快照失败（v%s）", snapshot_version, exc_info=True)
        return False
