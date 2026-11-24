"""M4 预算预留的数据库事务操作。"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AgentRun, GatewayBudgetAccount, GatewayBudgetReservation
from src.agent.budget_policy import BudgetSnapshot, validate_amount


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
