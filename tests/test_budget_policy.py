"""预算预留、未知费用和实际超额的领域行为。"""

import unittest
from decimal import Decimal

from src.agent.budget_policy import BudgetExceeded, BudgetSnapshot


class BudgetPolicyTests(unittest.TestCase):
    def test_new_admission_includes_existing_reservations(self):
        snapshot = BudgetSnapshot("USD", Decimal("10"), Decimal("3"), Decimal("0"))
        first = snapshot.admit(Decimal("4"))
        self.assertEqual(first.reserved, Decimal("4"))
        with self.assertRaises(BudgetExceeded) as error:
            first.admit(Decimal("4"))
        self.assertEqual(error.exception.code, "GATEWAY_BUDGET_EXCEEDED")

    def test_unknown_usage_keeps_reservation_until_confirmed(self):
        snapshot = BudgetSnapshot("USD", Decimal("10"), Decimal("0"), Decimal("4"))
        self.assertEqual(snapshot.settle(Decimal("4"), None), snapshot)
        settled = snapshot.settle(Decimal("4"), Decimal("2.25"))
        self.assertEqual((settled.spent, settled.reserved), (Decimal("2.25"), Decimal("0")))
        with self.assertRaises(ValueError):
            settled.settle(Decimal("4"), Decimal("2.25"))

    def test_actual_overage_is_recorded_instead_of_hidden(self):
        snapshot = BudgetSnapshot("USD", Decimal("10"), Decimal("7"), Decimal("3"))
        settled = snapshot.settle(Decimal("3"), Decimal("4"))
        self.assertEqual((settled.spent, settled.reserved), (Decimal("11"), Decimal("0")))
        with self.assertRaises(BudgetExceeded):
            settled.admit(Decimal("0.01"))

    def test_rejects_float_negative_and_excess_precision(self):
        snapshot = BudgetSnapshot("USD", Decimal("10"), Decimal("0"), Decimal("0"))
        for invalid in (1.0, Decimal("-1"), Decimal("0"), Decimal("0.0000000000001")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                snapshot.admit(invalid)
        with self.assertRaises(ValueError):
            BudgetSnapshot("usd", Decimal("10"), Decimal("0"), Decimal("0"))
        with self.assertRaises(ValueError):
            BudgetSnapshot("A1B", Decimal("10"), Decimal("0"), Decimal("0"))
        with self.assertRaises(ValueError):
            snapshot.admit(Decimal("1000000000000000000"))
