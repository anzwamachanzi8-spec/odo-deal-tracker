"""Tests for scoring.py and alert de-duplication.  Run: python -m unittest discover tests"""

import unittest

from odo_tracker import scoring, storage
from tests.test_storage import product

CFG = {"urgent_discount": 1.0, "hot_discount": 0.70,
       "was_price_jump_factor": 1.25, "was_price_vs_seen_price_factor": 5.0}


def scored(price, was_price, history=None, **extra):
    return scoring.score({**product(1, price, was_price), **extra}, history, CFG)


class ScoringTests(unittest.TestCase):
    def test_discount_formula(self):
        self.assertAlmostEqual(scoring.discount(300, 1000), 0.70)
        self.assertIsNone(scoring.discount(300, None))

    def test_tiers(self):
        self.assertEqual(scored(0, 260)["tier"], "URGENT")      # 100% off
        self.assertEqual(scored(0, None)["tier"], "URGENT")     # R0 even without a was-price
        self.assertEqual(scored(300, 1000)["tier"], "HOT")      # exactly 70%
        self.assertEqual(scored(301, 1000)["tier"], "NORMAL")   # 69.9%

    def test_missing_was_price_is_flagged_not_hidden(self):
        d = scored(500, None)
        self.assertEqual(d["tier"], "NORMAL")
        self.assertIn("no_was_price", d["flags"])
        self.assertTrue(d["suspicious"])

    def test_free_without_was_price(self):
        d = scored(0, None)
        self.assertIn("free_without_was_price", d["flags"])
        self.assertTrue(d["suspicious"])

    def test_was_price_jump_against_own_history(self):
        history = {"prices": [500], "was_prices": [1000, 1000]}
        self.assertNotIn("was_price_jumped", scored(500, 1000, history)["flags"])
        self.assertIn("was_price_jumped", scored(500, 2000, history)["flags"])

    def test_was_price_far_above_seen_prices(self):
        # Sold at R1,000 before; a "was R10,000" now is 10x anything we've seen.
        history = {"prices": [1000], "was_prices": []}
        self.assertIn("was_price_far_above_seen_prices", scored(900, 10000, history)["flags"])
        self.assertNotIn("was_price_far_above_seen_prices", scored(900, 2000, history)["flags"])

    def test_info_flags_dont_make_a_deal_suspicious(self):
        d = scored(0, 260, is_promo=True, has_price_range=True)
        self.assertEqual(d["flags"], ["promo", "price_range"])
        self.assertFalse(d["suspicious"])

    def test_rank_puts_unknown_discounts_last(self):
        deals = [scored(500, None), scored(100, 1000), scored(0, 200)]
        self.assertEqual([d["discount"] for d in scoring.rank(deals)], [1.0, 0.9, None])


class AlertDedupTests(unittest.TestCase):
    def test_same_item_same_price_alerts_once(self):
        conn = storage.connect(":memory:")
        deal = scored(0, 260)
        self.assertEqual(len(storage.not_yet_alerted(conn, [deal])), 1)
        storage.record_alert(conn, deal, "t1")
        self.assertEqual(storage.not_yet_alerted(conn, [deal]), [])
        # Same item at a new price is a new deal.
        self.assertEqual(len(storage.not_yet_alerted(conn, [scored(10, 260)])), 1)


if __name__ == "__main__":
    unittest.main()
