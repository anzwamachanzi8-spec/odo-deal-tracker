"""Tests for storage.py using made-up products and an in-memory database.

Run from the project folder:  python -m unittest discover tests
"""

import unittest

from odo_tracker import storage


def product(pid, price, was_price=1000.0, name=None):
    return {"product_id": pid, "name": name or f"Item {pid}", "brand": "Brand", "category": "cat",
            "section": "Today's Deals", "price": price, "was_price": was_price,
            "has_price_range": False, "is_sold_out": False, "is_promo": False,
            "deal_ends": None, "url": f"https://example.test/{pid}", "fetched_at": "2026-11-01T10:00:00+00:00"}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")  # throwaway database that lives in RAM

    def test_first_run_reports_no_changes(self):
        summary = storage.save_run(self.conn, "full", [product(1, 500)], "t1")
        self.assertTrue(summary["first_run"])
        self.assertEqual(summary["changes"], [])

    def test_detects_new_removed_and_price_change(self):
        storage.save_run(self.conn, "full", [product(1, 500), product(2, 300), product(3, 100)], "t1")
        summary = storage.save_run(self.conn, "full", [
            product(1, 500),            # unchanged
            product(2, 0),              # price dropped to R0
            product(4, 50),             # new
        ], "t2")                        # product 3 is gone
        kinds = {(c["product_id"], c["change_type"]) for c in summary["changes"]}
        self.assertEqual(kinds, {(2, "price_change"), (4, "new"), (3, "removed")})

    def test_item_seen_earlier_is_returned_not_new(self):
        storage.save_run(self.conn, "full", [product(1, 500), product(2, 300)], "t1")
        storage.save_run(self.conn, "full", [product(1, 500)], "t2")           # 2 drops out of view
        summary = storage.save_run(self.conn, "full", [product(1, 500), product(2, 300)], "t3")
        self.assertEqual([(c["product_id"], c["change_type"]) for c in summary["changes"]], [(2, "returned")])

    def test_was_price_change_counts_as_price_change(self):
        storage.save_run(self.conn, "full", [product(1, 500, was_price=1000)], "t1")
        summary = storage.save_run(self.conn, "full", [product(1, 500, was_price=5000)], "t2")
        self.assertEqual([c["change_type"] for c in summary["changes"]], ["price_change"])

    def test_modes_are_compared_separately(self):
        # A fast run only sees today's deals; it must not mark full-run items as removed.
        storage.save_run(self.conn, "full", [product(1, 500), product(2, 300)], "t1")
        summary = storage.save_run(self.conn, "fast", [product(1, 500)], "t2")
        self.assertTrue(summary["first_run"])

    def test_snapshots_are_appended_not_overwritten(self):
        for price in (500, 500, 400, 500):
            storage.save_run(self.conn, "full", [product(1, price)], "t")
        count = self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE product_id = 1").fetchone()[0]
        self.assertEqual(count, 4)
        # Same price twice in a row = one period; going back to R500 later = a new period.
        periods = storage.price_history(self.conn, 1)
        self.assertEqual([(p["price"], p["times_seen"]) for p in periods], [(500, 2), (400, 1), (500, 1)])


if __name__ == "__main__":
    unittest.main()
