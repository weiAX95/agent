"""M3 费用领域契约；缺少供应商用量时不估算或写入零费用。"""

from dataclasses import dataclass
from decimal import Decimal


MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None

    def __post_init__(self):
        for value in (self.input_tokens, self.output_tokens, self.cached_input_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token 用量必须是非负整数或未知")
        if (self.input_tokens is not None and self.cached_input_tokens is not None
                and self.cached_input_tokens > self.input_tokens):
            raise ValueError("缓存输入 token 不能超过输入 token 总数")


@dataclass(frozen=True)
class PriceVersion:
    """不可变价格快照；数值表示每百万 token 的货币金额。"""

    version_id: str
    currency: str
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal

    def __post_init__(self):
        if not self.version_id or len(self.version_id) > 128:
            raise ValueError("价格版本标识无效")
        if len(self.currency) != 3 or not all("A" <= char <= "Z" for char in self.currency):
            raise ValueError("币种必须是三个大写 ASCII 字母")
        for value in (self.input_per_million, self.output_per_million,
                      self.cached_input_per_million):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("价格必须是非负有限 Decimal")


def calculate_cost(usage: TokenUsage, price: PriceVersion) -> Decimal | None:
    """仅在三项用量都有供应商确认值时计算；0 与未知严格区分。"""
    if (usage.input_tokens is None or usage.output_tokens is None
            or usage.cached_input_tokens is None):
        return None
    # 输入总数包含缓存部分；按不同费率拆开计算，禁止重复计费。
    return (
        Decimal(usage.input_tokens - usage.cached_input_tokens) * price.input_per_million
        + Decimal(usage.cached_input_tokens) * price.cached_input_per_million
        + Decimal(usage.output_tokens) * price.output_per_million
    ) / MILLION
