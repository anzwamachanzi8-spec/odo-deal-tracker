"""Tests for the scheduled run: failure counting, pausing, heartbeat, the cloud's
"data changed" flag, and converting an old database.

Uses a temporary data folder and fake senders, so nothing touches the real site or database.
Run: python -m unittest discover -s tests -t .
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import main
from odo_tracker import state as run_state
from odo_tracker import storage
from odo_tracker.config import load_config
from odo_tracker.http_client import BlockedError
from scripts import migrate_db
from tests.test_storage import product


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.config = load_config()
        self.config["paths"]["data_dir"] = tmp
        self.config["paths"]["log_dir"] = tmp
        # Make cmd_run use our temporary config, and stop it writing a real log/sending pushes.
        mock.patch.object(main, "load_config", return_value=self.config).start()
        mock.patch.object(main, "start_logging").start()
        self.push = mock.patch.object(main.Notifier, "push").start()
        self.addCleanup(mock.patch.stopall)

    def run_tick(self, outcome):
        """One scheduled tick where run_fetch raises outcome (an exception) or returns it."""
        fake = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
        with mock.patch.object(main, "run_fetch", **fake):
            try:
                main.cmd_run(None)
            except SystemExit:
                pass

    def test_alert_after_three_failures_then_recovered(self):
        for _ in range(4):
            self.run_tick(RuntimeError("site down"))
        self.assertEqual(run_state.load(self.config)["consecutive_failures"], 4)
        self.assertEqual(self.push.call_count, 1)  # only at the 3rd failure
        self.assertIn("3 failed runs", self.push.call_args.args[0])

        with mock.patch.object(main, "send_digest_if_due"), mock.patch.object(main, "send_heartbeat_if_due"):
            self.run_tick({"alerts": 0, "data_changed": False})  # success
        self.assertEqual(run_state.load(self.config)["consecutive_failures"], 0)
        self.assertIn("recovered", self.push.call_args.args[0])

    def test_blocked_pauses_tracker_until_resume(self):
        self.run_tick(BlockedError("403"))
        self.assertIn("paused_reason", run_state.load(self.config))
        fetch = mock.Mock()
        with mock.patch.object(main, "run_fetch", fetch):
            main.cmd_run(None)
        fetch.assert_not_called()  # paused: no requests at all
        main.cmd_resume(None)
        self.assertNotIn("paused_reason", run_state.load(self.config))


    def test_changed_flag_written_only_when_data_changed(self):
        flag = self.config["paths"]["data_dir"] / "needs_commit"
        with mock.patch.dict(os.environ, {"ODO_CHANGED_FLAG": str(flag)}), \
             mock.patch.object(main, "send_heartbeat_if_due"):
            with mock.patch.object(main, "run_fetch", return_value={"alerts": 0, "data_changed": False}):
                main.cmd_run(None)
            self.assertFalse(flag.exists())
            with mock.patch.object(main, "run_fetch", return_value={"alerts": 1, "data_changed": True}), \
                 mock.patch.object(main, "send_digest_if_due"):
                main.cmd_run(None)
            self.assertTrue(flag.exists())

    def test_heartbeat_once_a_day_with_counts(self):
        run_state.save(self.config, {"hb_runs": 280, "hb_failed": 2, "hb_alerts": 3})
        self.push.return_value = True
        late = datetime(2026, 11, 2, 9, 0, tzinfo=main.SAST)
        with mock.patch.object(main, "datetime", wraps=datetime) as fake_dt:
            fake_dt.now.return_value = late
            main.send_heartbeat_if_due(self.config, main.Notifier(self.config))
            main.send_heartbeat_if_due(self.config, main.Notifier(self.config))  # same day: no repeat
        self.assertEqual(self.push.call_count, 1)
        self.assertIn("280 successful runs, 2 failed, 3 deal alerts", self.push.call_args.args[1])
        self.assertEqual(run_state.load(self.config)["hb_runs"], 0)

    def test_no_heartbeat_before_its_time(self):
        early = datetime(2026, 11, 2, 7, 0, tzinfo=main.SAST)
        with mock.patch.object(main, "datetime", wraps=datetime) as fake_dt:
            fake_dt.now.return_value = early
            main.send_heartbeat_if_due(self.config, main.Notifier(self.config))
        self.push.assert_not_called()


class MigrationTests(unittest.TestCase):
    def test_old_database_converts_and_reads_back_the_same(self):
        tmp = Path(tempfile.mkdtemp())
        old_path, new_path = tmp / "old.db", tmp / "new.db"
        old = sqlite3.connect(old_path)
        old.executescript(OLD_SCHEMA)
        runs = [(1, "full", [product(1, 500), product(2, 300)]),
                (2, "fast", [product(1, 500)]),
                (3, "full", [product(1, 400), product(2, 300)]),
                (4, "full", [product(2, 300)])]
        for run_id, mode, items in runs:
            old.execute("INSERT INTO runs VALUES (?, 't', ?, ?)", (run_id, mode, len(items)))
            old.executemany(f"INSERT INTO snapshots VALUES (?, {', '.join('?' * 13)})",
                            [(run_id, *[p[c] for c in storage.SNAPSHOT_COLUMNS]) for p in items])
        old.execute("INSERT INTO alerts VALUES (1, 400, 'HOT', 't')")
        old.commit()
        old.close()

        migrate_db.migrate(old_path, new_path)  # exits with an error if any run doesn't match
        new = storage.connect(new_path)
        self.assertEqual(new.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 3)  # was 7 rows
        self.assertEqual(new.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        # New runs continue numbering after the old ones.
        self.assertEqual(storage.save_run(new, "full", [product(2, 300)], "t")["run_id"], 5)


# The old (format 1) tables, enough for the migration test.
OLD_SCHEMA = """
CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, mode TEXT, item_count INTEGER);
CREATE TABLE snapshots (run_id INTEGER, product_id INTEGER, name TEXT, brand TEXT, category TEXT, section TEXT,
    price REAL, was_price REAL, has_price_range INTEGER, is_sold_out INTEGER, is_promo INTEGER, deal_ends TEXT,
    url TEXT, fetched_at TEXT, PRIMARY KEY (run_id, product_id));
CREATE TABLE changes (run_id INTEGER, product_id INTEGER, change_type TEXT, old_price REAL, new_price REAL,
    old_was_price REAL, new_was_price REAL);
CREATE TABLE alerts (product_id INTEGER, price REAL, tier TEXT, alerted_at TEXT, PRIMARY KEY (product_id, price));
"""


if __name__ == "__main__":
    unittest.main()
