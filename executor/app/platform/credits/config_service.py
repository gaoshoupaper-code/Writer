"""CreditConfigService — 暗调参数读取 + 内存缓存（AD11）。

从 credit_config 表读参数，缓存在内存中，避免每次 model_call 都查库。
管理页修改参数后调 reload() 刷新缓存。

暗调旋钮（D20）：
- output_token_weight / input_miss_weight / input_hit_weight：D7 三档权重
- credits_per_1k_tokens：标准 token → 积分单价（主暗调旋钮）
- tier_hold_amounts：D14 六档预扣额度
- max_debt：D27 负债上限（触及强停）
"""
from __future__ import annotations

import json
import threading
from typing import Any

from app.platform.core.db import CreditConfigRepository, Database


class CreditConfigService:
    """积分暗调参数服务（进程级单例，内存缓存 + 按需刷新）。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._cache: dict[str, str] | None = None

    def _ensure_loaded(self) -> dict[str, str]:
        """惰性加载缓存（首次访问时读库）。"""
        if self._cache is None:
            with self._lock:
                if self._cache is None:
                    repo = CreditConfigRepository(self._db)
                    self._cache = repo.get_all()
        return self._cache

    def reload(self) -> None:
        """管理页改参数后调此方法刷新缓存。"""
        with self._lock:
            repo = CreditConfigRepository(self._db)
            self._cache = repo.get_all()

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._ensure_loaded().get(key, default)

    def set(self, key: str, value: str) -> None:
        """写库 + 刷新缓存。"""
        repo = CreditConfigRepository(self._db)
        repo.set(key, value)
        self.reload()

    # ── 类型化的便捷读取 ──────────────────────────────────

    @property
    def output_token_weight(self) -> float:
        return float(self.get("output_token_weight", "2.0"))

    @property
    def input_miss_weight(self) -> float:
        return float(self.get("input_miss_weight", "1.0"))

    @property
    def input_hit_weight(self) -> float:
        return float(self.get("input_hit_weight", "0.01"))

    @property
    def credits_per_1k_tokens(self) -> float:
        return float(self.get("credits_per_1k_tokens", "1.0"))

    @property
    def tier_hold_amounts(self) -> list[int]:
        """六档预扣额度 [档1, 档2, ..., 档6]。"""
        raw = self.get("tier_hold_amounts", "[500,1500,3500,7000,10000,13000]")
        return json.loads(raw)

    @property
    def max_debt(self) -> int:
        """最大负债额度（负数，如 -5000）。余额触及此值强停（D27）。"""
        return int(self.get("max_debt", "-5000"))

    # ── 核心计算：token usage → 消耗积分 ─────────────────

    def calculate_credits(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> int:
        """按 D7 三档权重把 token usage 折算成消耗积分。

        - input_tokens: 输入 token 总数
        - output_tokens: 输出 token 总数
        - cached_tokens: 输入中缓存命中的 token 数（deepseek prompt caching）

        标准token = output×output_weight + (input-cached)×input_miss_weight
                    + cached×input_hit_weight
        积分 = 标准token / 1000 × credits_per_1k_tokens

        返回消耗的积分数（≥0 整数，向上取整）。
        """
        input_miss = max(0, input_tokens - cached_tokens)
        standard_tokens = (
            output_tokens * self.output_token_weight
            + input_miss * self.input_miss_weight
            + cached_tokens * self.input_hit_weight
        )
        credits = standard_tokens / 1000.0 * self.credits_per_1k_tokens
        return max(0, int(credits + 0.5))  # 四舍五入到整数

    def tier_hold_amount(self, tier: int) -> int:
        """按篇幅档位（1-6）取预扣额度。超出范围取最近端。"""
        amounts = self.tier_hold_amounts
        if tier < 1:
            return amounts[0]
        if tier > len(amounts):
            return amounts[-1]
        return amounts[tier - 1]

    def get_all_for_display(self) -> dict[str, Any]:
        """管理页展示用：返回所有参数 + 描述。"""
        repo = CreditConfigRepository(self._db)
        rows = self._db.conn.execute(
            "SELECT key, value, description, updated_at FROM credit_config ORDER BY key"
        ).fetchall()
        return {
            r["key"]: {
                "value": r["value"],
                "description": r["description"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        }
