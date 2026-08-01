"""不可变 Harness candidate 的装配与身份门禁。

单阶段发版下 probe_candidate 是唯一门禁：让拟发布 executor 在 candidate 的干净
checkout 上做真实最小装配，确认 harness_commit / assembled / artifact_snapshot
中间件齐全 + runtime identity 稳定。probe 通过即晋升 production。

旧的 validate_candidate_snapshot（要求 snapshot trace + 证据卷宗 + 评估卷宗三件套）
已移除——历史上从未有 snapshot 测试通过过，门禁永远卡死，导致 session 永远停在
pending_review。详见 publish_session docstring。
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.settings import settings


def probe_candidate(commit_hash: str) -> dict[str, Any]:
    """让拟发布 executor 从 candidate 干净 checkout 做真实最小装配。"""
    response = httpx.post(
        f"{settings.executor_url.rstrip('/')}/internal/harness/probe",
        json={"source_commit": commit_hash},
        timeout=120.0,
    )
    response.raise_for_status()
    result = response.json()
    if (
        result.get("harness_commit") != commit_hash
        or not result.get("assembled")
        or not result.get("artifact_snapshot_middleware")
    ):
        raise ValueError(f"candidate clean-checkout probe failed: {result}")
    return result


__all__ = ["probe_candidate"]
