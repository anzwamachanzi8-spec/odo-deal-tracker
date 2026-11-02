"""Tests for the scheduled run: failure counting, pausing, pruning.

Uses a temporary data folder and fake senders, so nothing touches the real site or database.
Run: python -m unittest discover -s tests -t .
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from odo_tracker import state as run_state
from odo_tracker import storage
from odo_tracker.config import load_config
from odo_tracker.http_client import BlockedError
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

    def run_tick(self, error):
        with mock.patch.object(main, "run_fetch", side_effect=error):
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

        with mock.patch.object(main, "send_digest_if_due"):
            self.run_tick(None)  # success
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


class PruneTests(unittest.TestCase):
    def test_old_fast_runs_deleted_full_runs_kept(self):
        conn = storage.connect(":memory:")
        storage.save_run(conn, "full", [product(1, 100)], "2026-10-01T10:00:00+00:00")
        storage.save_run(conn, "fast", [product(1, 100)], "2026-10-01T10:05:00+00:00")
        storage.save_run(conn, "fast", [product(1, 100)], "2026-10-01T10:10:00+00:00")
        storage.save_run(conn, "fast", [product(1, 100)], "2026-11-01T10:00:00+00:00")
        deleted = storage.prune_fast_runs(conn, 2, "2026-11-01T10:05:00+00:00")
        self.assertEqual(deleted, 2)
        modes = [r[0] for r in conn.execute("SELECT mode FROM runs ORDER BY run_id")]
        self.assertEqual(modes, ["full", "fast"])

    def test_newest_fast_run_is_never_pruned(self):
        conn = storage.connect(":memory:")
        storage.save_run(conn, "fast", [product(1, 100)], "2026-10-01T10:05:00+00:00")
        self.assertEqual(storage.prune_fast_runs(conn, 2, "2026-11-01T10:00:00+00:00"), 0)


if __name__ == "__main__":
    unittest.main()
