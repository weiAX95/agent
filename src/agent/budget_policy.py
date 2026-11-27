"""M4 预算领域契约；共享存储负责原子提交，领域层只计算允许的状态变化。"""

from dataclasses import dataclass
from decimal import Decimal


MAX_MONEY = Decimal(10) ** 18


class BudgetExceeded(RuntimeError):
    code = "GATEWAY_BUDGET_EXCEEDED"


def validate_amount(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("预算金额必须是有限 Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError("预算金额必须为正数" if positive else "预算金额不能为负数")
    if value.as_tuple().exponent < -12:
        raise ValueError("预算金额最多保留 12 位小数")
    if value >= MAX_MONEY:
        raise ValueError("预算金额超出数据库精度")
    return value


@dataclass(frozen=True)
class BudgetSnapshot:
    """同一币种、同一预算周期的持久快照；调用方必须在事务锁内使用。"""

    currency: str
    limit: Decimal
    spent: Decimal
    reserved: Decimal

    def __post_init__(self):
        if len(self.currency) != 3 or not all("A" <= char <= "Z" for char in self.currency):
            raise ValueError("币种必须是三个大写 ASCII 字母")
        for value in (self.limit, self.spent, self.reserved):
            validate_amount(value)
        if self.limit <= 0:
            raise ValueError("预算上限必须大于零")

    def admit(self, amount: Decimal) -> "BudgetSnapshot":
        """预留必须先于上游请求；持久层用行锁或等价 CAS 原子检查并写入。"""
        amount = validate_amount(amount, positive=True)
        if self.spent + self.reserved + amount > self.limit:
            raise BudgetExceeded(BudgetExceeded.code)
        return BudgetSnapshot(self.currency, self.limit, self.spent, self.reserved + amount)

    def settle(self, reserved_amount: Decimal, confirmed_cost: Decimal | None) -> "BudgetSnapshot":
        """未知费用继续占用预留；已确认费用即使超出预留也要如实入账。"""
        reserved_amount = validate_amount(reserved_amount, positive=True)
        if reserved_amount > self.reserved:
            raise ValueError("结算金额超过当前预留")
        if confirmed_cost is None:
            return self
        confirmed_cost = validate_amount(confirmed_cost)
        return BudgetSnapshot(
            self.currency, self.limit,
            self.spent + confirmed_cost,
            self.reserved - reserved_amount,
        )
