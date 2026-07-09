"""CreditsService — 积分计费核心业务逻辑（D4/D23/D27）。

职责：
- 预扣：访谈确认后按篇幅档位冻结积分（D13）
- 累加：每次 LLM 调用后累加消耗到 hold（D3）
- 强停检查：余额触及 max_debt（-5000）时触发（D27）
- 结算：创作结束时多退少补 + 落流水（D4/D23）
- 邀请码到账：注册时加积分（AD10）
- 管理员调整：手动加/减积分（D8）

数据流：
  create_hold → add_consumption (多次) → settle_hold
                                       ↓
                         汇总消耗 → 多退少补 → 落一条 creation_consume 流水
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.platform.core.db import (
    CreditHoldRepository,
    CreditTransactionRepository,
    Database,
    UserRepository,
)

from .config_service import CreditConfigService

logger = logging.getLogger("writer.credits")


class CreditsService:
    """积分计费服务（进程级单例）。"""

    def __init__(self, db: Database, config: CreditConfigService) -> None:
        self._db = db
        self._config = config
        self._users = UserRepository(db)
        self._holds = CreditHoldRepository(db)
        self._txs = CreditTransactionRepository(db)

    # ── 预扣（D4/D13）──────────────────────────────────────

    def create_hold(
        self, *, user_id: str, thread_id: str, trace_id: str | None, tier: int,
    ) -> dict | None:
        """访谈确认后创建预扣。

        逻辑（D4 事前预扣）：
        1. 按档位取预扣额度（D14）
        2. 检查余额：余额 - 预扣额度 必须 > max_debt（否则拒绝启动）
        3. 冻结积分（从余额扣除预扣额度）
        4. 创建 hold 记录

        返回 hold dict，或 None（余额不足拒绝启动）。
        """
        hold_amount = self._config.tier_hold_amount(tier)
        max_debt = self._config.max_debt

        balance = self._users.get_credits(user_id)
        # D27: 余额 - 预扣 > max_debt 才允许启动（否则一开始就触及负债上限）
        if balance - hold_amount < max_debt:
            logger.warning(
                "预扣失败：user=%s balance=%d hold=%d max_debt=%d",
                user_id, balance, hold_amount, max_debt,
            )
            return None

        # 冻结：从余额扣除预扣额度
        new_balance = self._users.adjust_credits(user_id, -hold_amount)
        hold = self._holds.create(
            user_id=user_id, thread_id=thread_id, trace_id=trace_id,
            tier=tier, held_amount=hold_amount,
        )
        logger.info(
            "预扣创建：user=%s hold=%s tier=%d amount=%d balance=%d→%d",
            user_id, hold["hold_id"], tier, hold_amount, balance, new_balance,
        )
        return hold

    def get_active_hold(self, thread_id: str) -> dict | None:
        """取某 thread 当前活跃的预扣。"""
        return self._holds.get_active_by_thread(thread_id)

    # ── 累加消耗（D3）──────────────────────────────────────

    def add_consumption(self, hold_id: str, credits: int) -> dict | None:
        """累加一次 LLM 调用的消耗到 hold。

        credits 由 CreditConfigService.calculate_credits 算出（D7 三档权重）。
        """
        if credits <= 0:
            return self._holds.get(hold_id)
        return self._holds.add_consumed(hold_id, credits)

    def check_credit_limit(self, user_id: str, hold: dict) -> bool:
        """检查是否触及负债上限（D27）。

        实时余额 = 当前余额（已扣预扣）- hold.consumed（已消耗但未结算）。
        若实时余额 ≤ max_debt，返回 True（应强停）。

        注意：预扣时已从余额扣了 held_amount，consumed 在 hold 内累加但不再扣余额
        （余额在 settle 时统一结算）。所以实时余额 = balance - consumed。
        """
        balance = self._users.get_credits(user_id)
        consumed = hold.get("consumed", 0)
        effective = balance - consumed
        return effective <= self._config.max_debt

    # ── 结算（D4 多退少补 / D23 汇总一条流水）──────────────

    def settle_hold(self, hold_id: str, *, force_stopped: bool = False) -> dict | None:
        """创作结束时结算预扣。

        逻辑（D4 多退少补）：
        - 实际消耗 = hold.consumed
        - 预扣了 held_amount，实际消耗 consumed
        - 如果 consumed < held_amount：退还差额（余额加回 held_amount - consumed）
        - 如果 consumed > held_amount：补扣差额（余额再扣 consumed - held_amount）
        - 落一条 creation_consume 流水（D23 按创作汇总一条）

        force_stopped=True 时（D27 强停），status 记为 force_stopped。
        """
        hold = self._holds.get(hold_id)
        if hold is None or hold["status"] != "active":
            return hold

        held = hold["held_amount"]
        consumed = hold["consumed"]
        user_id = hold["user_id"]
        thread_id = hold["thread_id"]

        if consumed < held:
            # 退还差额
            refund = held - consumed
            balance_after = self._users.adjust_credits(user_id, refund)
            self._txs.create(
                user_id=user_id, type="creation_consume", amount=-consumed,
                balance_after=balance_after, ref_thread_id=thread_id,
                ref_hold_id=hold_id, note=f"创作消耗（预扣{held}，实际{consumed}，退还{refund}）",
                created_by="system",
            )
            logger.info(
                "结算退还：hold=%s consumed=%d < held=%d refund=%d balance=%d",
                hold_id, consumed, held, refund, balance_after,
            )
        elif consumed > held:
            # 补扣差额
            extra = consumed - held
            balance_after = self._users.adjust_credits(user_id, -extra)
            self._txs.create(
                user_id=user_id, type="creation_consume", amount=-consumed,
                balance_after=balance_after, ref_thread_id=thread_id,
                ref_hold_id=hold_id, note=f"创作消耗（预扣{held}，实际{consumed}，补扣{extra}）",
                created_by="system",
            )
            logger.info(
                "结算补扣：hold=%s consumed=%d > held=%d extra=%d balance=%d",
                hold_id, consumed, held, extra, balance_after,
            )
        else:
            # 刚好
            balance_after = self._users.get_credits(user_id)
            self._txs.create(
                user_id=user_id, type="creation_consume", amount=-consumed,
                balance_after=balance_after, ref_thread_id=thread_id,
                ref_hold_id=hold_id, note=f"创作消耗（预扣{held}，实际{consumed}）",
                created_by="system",
            )

        status = "force_stopped" if force_stopped else "settled"
        return self._holds.settle(hold_id, status)

    # ── 邀请码到账（AD10）──────────────────────────────────

    def grant_invite_credits(
        self, *, user_id: str, amount: int, invite_code: str,
    ) -> int:
        """注册时邀请码积分到账。返回操作后余额。"""
        balance_after = self._users.adjust_credits(user_id, amount)
        self._txs.create(
            user_id=user_id, type="invite_grant", amount=amount,
            balance_after=balance_after, note=f"邀请码到账：{invite_code}",
            created_by="system",
        )
        logger.info("邀请码到账：user=%s code=%s amount=%d balance=%d",
                     user_id, invite_code, amount, balance_after)
        return balance_after

    # ── 管理员调整（D8）────────────────────────────────────

    def admin_adjust(
        self, *, user_id: str, amount: int, note: str, admin_id: str,
    ) -> int:
        """管理员手动调整积分。返回操作后余额。"""
        balance_after = self._users.adjust_credits(user_id, amount)
        self._txs.create(
            user_id=user_id, type="admin_adjust", amount=amount,
            balance_after=balance_after, note=note, created_by=admin_id,
        )
        logger.info("管理员调整：user=%s admin=%s amount=%d balance=%d note=%s",
                     user_id, admin_id, amount, balance_after, note)
        return balance_after

    # ── 查询 ───────────────────────────────────────────────

    def get_balance(self, user_id: str) -> int:
        return self._users.get_credits(user_id)

    def is_frozen(self, user_id: str) -> bool:
        """D16/D26：余额 ≤ 0 则冻结（只读，不可触发 LLM）。"""
        return self._users.get_credits(user_id) <= 0

    def list_user_transactions(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._txs.list_by_user(user_id, limit)

    def list_all_transactions(self, limit: int = 100) -> list[dict]:
        return self._txs.list_all(limit)

    def list_user_holds(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._holds.list_by_user(user_id, limit)


# ════════════════════════════════════════════════════════════════
# 进程级单例（启动时 init_credits_service 注入，运行时 get_credits_service 取用）
# ════════════════════════════════════════════════════════════════

_credits_service: CreditsService | None = None


def init_credits_service(service: CreditsService) -> None:
    global _credits_service
    _credits_service = service


def get_credits_service() -> CreditsService:
    if _credits_service is None:
        raise RuntimeError("CreditsService not initialized; call init_credits_service() first")
    return _credits_service
