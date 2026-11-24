"""聊天 Run 在模型发送前申请共享准入和持久预算。"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.budget_storage import reserve_run_budget, settle_run_budget_from_usage
from api.models import AgentRun
from api.redis_admission import RedisEvaluator, acquire, release
from src.agent.budget_policy import BudgetSnapshot, validate_amount
from src.agent.run_policy import RunStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayAdmissionPolicy:
    rpm_limit: int
    concurrent_limit: int
    currency: str
    reservation_amount: Decimal

    def __post_init__(self):
        if type(self.rpm_limit) is not int or not 1 <= self.rpm_limit <= 100_000:
            raise ValueError("M4 RPM 配置无效")
        if type(self.concurrent_limit) is not int or not 1 <= self.concurrent_limit <= 10_000:
            raise ValueError("M4 并发配置无效")
        validate_amount(self.reservation_amount, positive=True)
        if not isinstance(self.currency, str):
            raise ValueError("M4 币种配置无效")
        # 复用预算领域的币种规则，不接受小写或任意字符串。
        BudgetSnapshot(self.currency, self.reservation_amount, Decimal(0), Decimal(0))


class RunAdmission:
    def __init__(self, redis: RedisEvaluator, policy: GatewayAdmissionPolicy):
        self.redis = redis
        self.policy = policy

    async def admit(self, db: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID,
                    timeout_seconds: float) -> None:
        """先占 Redis 租约，再提交数据库预留；任何预留失败释放租约。"""
        lease_ms = min(3_660_000, max(1_000, math.ceil(timeout_seconds * 1000) + 60_000))
        await acquire(self.redis, owner_id, run_id, self.policy.rpm_limit,
                      self.policy.concurrent_limit, lease_ms)
        try:
            await reserve_run_budget(
                db, run_id, owner_id, self.policy.currency,
                datetime.now(UTC).date(), self.policy.reservation_amount,
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            with anyio.CancelScope(shield=True):
                try:
                    await release(self.redis, owner_id, run_id)
                except Exception:
                    # 租约 TTL 兜底；保留原始预算错误供调用方判断。
                    logger.exception("Could not release rejected gateway lease; run_id=%s", run_id)
            raise

    async def after_execution(self, db: AsyncSession, run_id: uuid.UUID,
                              owner_id: uuid.UUID) -> None:
        """终态结算并释放并发；恢复待核对的 Run 保留租约直到 TTL。"""
        try:
            run = await db.scalar(select(AgentRun).where(
                AgentRun.id == run_id, AgentRun.owner_id == owner_id,
            ))
            if run is None:
                raise ValueError("Run 不存在或不属于当前用户")
            if run.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED,
                                  RunStatus.CANCELLED, RunStatus.TIMED_OUT}:
                await db.rollback()
                return
            await db.rollback()
            try:
                await settle_run_budget_from_usage(db, run_id, owner_id)
                await db.commit()
            except Exception:
                await db.rollback()
                # 上游费用可能已经产生；结算失败保留预算预留并交给对账流程。
                logger.exception("Gateway budget settlement pending; run_id=%s", run_id)
            try:
                await release(self.redis, owner_id, run_id)
            except Exception:
                logger.exception("Gateway lease release pending TTL; run_id=%s", run_id)
        except BaseException:
            await db.rollback()
            raise
