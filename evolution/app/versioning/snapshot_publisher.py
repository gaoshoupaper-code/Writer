"""snapshot_publisher —— executor 通知层（去 DB 重构）。

发布逻辑已迁移到 evolve/api.publish_session（调 registry_repo + git_ops）。
本模块只保留 notify_executor：发版后 HTTP 通知执行端 reload。

通知机制（降级模式）：
  evolution 发布新版本 → POST executor /internal/snapshot/refreshed
  → 执行端 reload_current() 拉最新 main + 重新加载包
  通知彻底降级：executor_url 未配置/失败 → 静默吞掉（还有 reconcile 兜底）。
"""
from __future__ import annotations

import logging

from app.core.settings import settings

logger = logging.getLogger("evolution.snapshot_publisher")


def notify_executor(version: int) -> bool:
    """通知执行端「新 production 版本发布了」。

    彻底降级：
      - executor_url 未配置 → 不通知（返回 False，但不报错）
      - 网络失败/超时 → 静默吞掉（返回 False）
    executor 有 10 分钟 reconcile 兜底，通知丢失不会永久停在旧版。

    Args:
        version: 新发布的版本号

    Returns: True=通知成功，False=未配置或失败。
    """
    executor_url = getattr(settings, "executor_url", "").rstrip("/")
    if not executor_url:
        logger.debug("executor_url 未配置，跳过通知（reconcile 兜底）")
        return False
    try:
        import httpx
        resp = httpx.post(
            f"{executor_url}/internal/snapshot/refreshed",
            json={"version": version},
            timeout=2.0,
        )
        if resp.status_code < 300:
            logger.info("已通知执行端 v%s", version)
            return True
        logger.warning("通知执行端失败: HTTP %s", resp.status_code)
        return False
    except Exception:
        logger.debug("通知执行端失败（v%s）", version, exc_info=True)
        return False


__all__ = ["notify_executor"]
