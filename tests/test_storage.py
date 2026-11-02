"""Tests for storage.py using made-up products and an in-memory database.

Run from the project folder:  python -m unittest discover tests
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

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

    def test_only_changed_versions_are_saved_but_history_is_complete(self):
        for price in (500, 500, 400, 500):
            storage.save_run(self.conn, "full", [product(1, price)], "t")
        count = self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE product_id = 1").fetchone()[0]
        self.assertEqual(count, 3)  # the second R500 run repeated the first, so it added no row
        # Same price twice in a row = one period; going back to R500 later = a new period.
        periods = storage.price_history(self.conn, 1)
        self.assertEqual([(p["price"], p["times_seen"]) for p in periods], [(500, 2), (400, 1), (500, 1)])

    def test_products_in_run_rebuilds_each_run_exactly(self):
        runs = [[product(1, 500), product(2, 300)],
                [product(1, 450)],                            # 2 gone, 1 cheaper
                [product(1, 450), product(2, 300), product(3, 99)],
                [product(2, 250, name="Item 2 (renamed)")]]
        ids = [storage.save_run(self.conn, "full", r, "t")["run_id"] for r in runs]
        storage.save_run(self.conn, "fast", [product(1, 450)], "t")  # other mode doesn't interfere
        for run_id, expected in zip(ids, runs):
            got = {r["product_id"]: (r["price"], r["name"]) for r in storage.products_in_run(self.conn, run_id)}
            self.assertEqual(got, {p["product_id"]: (p["price"], p["name"]) for p in expected})

    def test_unchanged_run_adds_no_data_rows(self):
        storage.save_run(self.conn, "full", [product(1, 500), product(2, 300)], "t1")
        before = storage.content_counts(self.conn)
        summary = storage.save_run(self.conn, "full", [product(1, 500), product(2, 300)], "t2")
        self.assertEqual(summary["new_versions"], 0)
        self.assertEqual(storage.content_counts(self.conn), before)

    def test_old_format_database_is_refused(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE runs (run_id INTEGER PRIMARY KEY, started_at TEXT, mode TEXT, item_count INTEGER)")
        old.execute("INSERT INTO runs VALUES (1, 't', 'full', 0)")
        old.commit()
        old.close()
        with self.assertRaises(storage.OldDatabaseFormat):
            storage.connect(path)


if __name__ == "__main__":
    unittest.main()
