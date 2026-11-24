"""M4 预算预留的数据库事务操作。"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import (
    AgentRun, GatewayBudgetAccount, GatewayBudgetAdjustment,
    GatewayBudgetReservation,
)
from api.usage_storage import get_run_usage_summary
from src.agent.budget_policy import BudgetSnapshot, validate_amount
from src.agent.run_policy import RunStatus


class BudgetNotConfigured(RuntimeError):
    code = "GATEWAY_BUDGET_NOT_CONFIGURED"


async def reserve_run_budget(
    db: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID,
    currency: str, budget_date: date, amount: Decimal,
) -> GatewayBudgetReservation:
    """先锁 Run 再锁预算账户；同一 Run 重试不重复预留或改变原金额。"""
    if not isinstance(budget_date, date) or isinstance(budget_date, datetime):
        raise ValueError("预算日期必须是 UTC 日期")
    validate_amount(amount, positive=True)
    run = await db.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.owner_id == owner_id,
    ).with_for_update())
    if run is None:
        raise ValueError("Run 不存在或不属于当前用户")
    existing = await db.scalar(select(GatewayBudgetReservation).where(
        GatewayBudgetReservation.run_id == run_id,
    ))
    if existing is not None:
        account = await db.get(GatewayBudgetAccount, existing.account_id)
        if (existing.status != "reserved" or existing.reserved_amount != amount
                or account.currency != currency or account.budget_date != budget_date):
            raise ValueError("同一 Run 的预算预留参数冲突")
        return existing
    account = await db.scalar(select(GatewayBudgetAccount).where(
        GatewayBudgetAccount.owner_id == owner_id,
        GatewayBudgetAccount.currency == currency,
        GatewayBudgetAccount.budget_date == budget_date,
    ).with_for_update())
    if account is None:
        raise BudgetNotConfigured(BudgetNotConfigured.code)
    snapshot = BudgetSnapshot(currency, account.limit_amount,
                              account.spent_amount, account.reserved_amount)
    admitted = snapshot.admit(amount)
    account.reserved_amount = admitted.reserved
    account.updated_at = datetime.now(UTC)
    reservation = GatewayBudgetReservation(
        id=uuid.uuid4(), run_id=run_id, account_id=account.id,
        reserved_amount=amount, status="reserved",
    )
    db.add(reservation)
    await db.flush()
    return reservation


async def settle_run_budget_from_usage(
    db: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID,
) -> GatewayBudgetReservation:
    """从不可变回执重算整次 Run 费用；迟到修正只入账差额。"""
    # 与预留使用相同锁顺序，避免并发结算、预留和迟到回执相互穿透。
    run = await db.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.owner_id == owner_id,
    ).with_for_update())
    if run is None:
        raise ValueError("Run 不存在或不属于当前用户")
    reservation = await db.scalar(select(GatewayBudgetReservation).where(
        GatewayBudgetReservation.run_id == run_id,
    ).with_for_update())
    if reservation is None:
        raise BudgetNotConfigured("Run 尚未预留预算")
    account = await db.scalar(select(GatewayBudgetAccount).where(
        GatewayBudgetAccount.id == reservation.account_id,
        GatewayBudgetAccount.owner_id == owner_id,
    ).with_for_update())
    if account is None:
        raise ValueError("预算账户不存在或归属错误")
    if run.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED,
                          RunStatus.CANCELLED, RunStatus.TIMED_OUT}:
        return reservation
    summary = await get_run_usage_summary(db, run_id, owner_id)
    if set(summary.costs_by_currency) - {account.currency}:
        raise ValueError("模型费用币种与预算账户不一致")
    if summary.unknown_cost_call_count:
        if reservation.status == "settled":
            raise ValueError("已结算 Run 出现未知费用调用，需人工核对")
        # 只要一次物理调用费用未知，整笔预留继续占用，不能当作零费用释放。
        return reservation
    confirmed_cost = validate_amount(
        summary.costs_by_currency.get(account.currency, Decimal(0)),
    )
    previous_cost = reservation.confirmed_cost or Decimal(0)
    if reservation.status == "settled" and previous_cost == confirmed_cost:
        return reservation
    now = datetime.now(UTC)
    if reservation.status == "reserved":
        snapshot = BudgetSnapshot(account.currency, account.limit_amount,
                                  account.spent_amount, account.reserved_amount)
        settled = snapshot.settle(reservation.reserved_amount, confirmed_cost)
        account.spent_amount = settled.spent
        account.reserved_amount = settled.reserved
        reservation.status = "settled"
    else:
        # 修正回执可减少或增加历史费用；不可重新消费预留，也不可产生负余额。
        account.spent_amount = validate_amount(account.spent_amount + confirmed_cost - previous_cost)
    reservation.confirmed_cost = confirmed_cost
    reservation.updated_at = now
    account.updated_at = now
    db.add(GatewayBudgetAdjustment(
        id=uuid.uuid4(), reservation_id=reservation.id,
        previous_cost=previous_cost, confirmed_cost=confirmed_cost,
        delta=confirmed_cost - previous_cost, created_at=now,
    ))
    await db.flush()
    return reservation
