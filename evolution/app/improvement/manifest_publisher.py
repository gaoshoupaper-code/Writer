"""manifest 发布器（Phase 6 T3.3）。

封装「surface A/B 批准 → 聚合发布 production manifest → 通知执行端」完整流程。

职责边界：
  - approve_and_publish：surface 版本标 approved + 发布新 manifest + 通知执行端
  - publish_only：只发布 manifest（surface 已 approved 时用）
  - notify_executor：HTTP 通知执行端 manifest 变更（彻底降级，失败静默）

与 manifest_repo 的分工：
  - manifest_repo.publish_production：纯聚合 + 全局锁（数据层，无副作用）
  - manifest_publisher：加上「approve + 通知」的业务编排（服务层）

通知机制（决策 D5：manifest 统一接管，替代旧 prompt refreshed）：
  evolution 发布新 manifest → POST executor /internal/manifest/refreshed
  → 执行端标记 manifest 缓存 stale → 下次 assemble 强制重拉
  通知是彻底降级的：executor_url 未配置/网络失败 → 静默吞掉。
  执行端下次启动或下次请求会自然拉到新 manifest，单次通知丢失只影响及时性。

设计依据：设计文档 D7（approved 聚合）+ D12（全局锁）+ D5（manifest 接管）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.settings import settings
from app.improvement import manifest_repo, surface_repo

logger = logging.getLogger("evolution.manifest_publisher")


def approve_and_publish(surface_version_id: int) -> dict[str, Any] | None:
    """批准一个 surface 版本并发布新 production manifest（A/B 胜出后调用）。

    流程：
      1. surface 版本标 approved（status 流转最后一环）
      2. 聚合当前所有 approved surface → 新 production manifest（全局锁）
      3. 通知执行端 manifest 变更

    Args:
        surface_version_id: 要批准的 surface_versions.id

    Returns: {surface_version_id, manifest_version, notified} 或 None（surface 不存在）。
    """
    ver = surface_repo.get_version_by_id(surface_version_id)
    if ver is None:
        logger.error("surface 版本不存在: %s", surface_version_id)
        return None

    # 1. 标 approved
    surface_repo.approve(surface_version_id)
    logger.info(
        "surface %s/%s/%s v%s approved",
        ver["surface_type"], ver["surface_name"], ver["scope"], ver["version"],
    )

    # 2. 聚合发布
    manifest = manifest_repo.publish_production()
    if manifest is None:
        logger.error("发布 manifest 失败（无 approved surface？不应发生，刚 approve 过）")
        return {"surface_version_id": surface_version_id, "manifest_version": None, "notified": False}

    # 3. 通知执行端
    notified = notify_executor(manifest["manifest_version"])
    return {
        "surface_version_id": surface_version_id,
        "manifest_version": manifest["manifest_version"],
        "notified": notified,
    }


def publish_only() -> dict[str, Any] | None:
    """只发布 manifest（不 approve 新 surface，用现有 approved 集合）。

    场景：手动重新聚合（如修正了多个 surface 的 approved 状态后统一发布）。
    """
    manifest = manifest_repo.publish_production()
    if manifest is None:
        logger.warning("发布失败：无 approved surface")
        return None
    notified = notify_executor(manifest["manifest_version"])
    return {"manifest_version": manifest["manifest_version"], "notified": notified}


def notify_executor(manifest_version: int) -> bool:
    """通知执行端「新 production manifest 发布了」。

    彻底降级：
      - executor_url 未配置 → 不通知（返回 False，但不报错）
      - 网络失败/超时 → 静默吞掉（返回 False）
    执行端下次启动或下次请求会自然拉到新 manifest，单次通知丢失只影响及时性。

    Returns: True=通知成功，False=未配置或失败。
    """
    executor_url = getattr(settings, "executor_url", "").rstrip("/")
    if not executor_url:
        logger.debug("executor_url 未配置，跳过 manifest 通知")
        return False
    try:
        import httpx
        resp = httpx.post(
            f"{executor_url}/internal/manifest/refreshed",
            json={"manifest_version": manifest_version},
            timeout=2.0,
        )
        if resp.status_code < 300:
            logger.info("已通知执行端 manifest v%s", manifest_version)
            return True
        logger.warning("通知执行端 manifest 失败: HTTP %s", resp.status_code)
        return False
    except Exception:
        logger.debug("通知执行端 manifest 失败（v%s）", manifest_version, exc_info=True)
        return False
