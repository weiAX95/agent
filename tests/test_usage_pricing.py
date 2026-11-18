"""M3 费用精度和未知用量的离线领域测试。"""

import unittest
from decimal import Decimal

from src.agent.usage_pricing import PriceVersion, TokenUsage, calculate_cost


class UsagePricingTests(unittest.TestCase):
    def setUp(self):
        self.price = PriceVersion(
            version_id="test-v1", currency="USD",
            input_per_million=Decimal("2.50"),
            output_per_million=Decimal("10.00"),
            cached_input_per_million=Decimal("0.25"),
        )

    def test_cache_is_not_charged_twice_and_decimal_is_exact(self):
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=200_000,
                           cached_input_tokens=400_000)
        self.assertEqual(calculate_cost(usage, self.price), Decimal("3.600000"))

    def test_missing_provider_usage_keeps_cost_unknown(self):
        for usage in (
            TokenUsage(None, 0, 0), TokenUsage(0, None, 0), TokenUsage(0, 0, None),
        ):
            with self.subTest(usage=usage):
                self.assertIsNone(calculate_cost(usage, self.price))
        self.assertEqual(calculate_cost(TokenUsage(0, 0, 0), self.price), Decimal(0))

    def test_invalid_tokens_and_float_prices_are_rejected(self):
        for args in ((-1, 0, 0), (1, 0, 2), (True, 0, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                TokenUsage(*args)
        with self.assertRaises(ValueError):
            PriceVersion("test-v2", "USD", 2.5, Decimal(1), Decimal(0))
        with self.assertRaises(ValueError):
            PriceVersion("test-v2", "U1D", Decimal(1), Decimal(1), Decimal(0))

    def test_price_version_is_immutable(self):
        with self.assertRaises(AttributeError):
            self.price.input_per_million = Decimal("99")


if __name__ == "__main__":
    unittest.main()
