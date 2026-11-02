"""Tests for notify.py. Real senders are replaced with fakes (mock), so nothing is sent.

Run: python -m unittest discover -s tests -t .
"""

import unittest
from unittest import mock

from odo_tracker import notify, storage
from tests.test_scoring import scored

CONFIG = {"notifications": {
    "confirm_urgent_on_product_page": True, "hot_batch_threshold": 3,
    "ntfy": {"enabled": True, "server": "https://ntfy.invalid"},
    "email": {"enabled": True, "to": ["me@example.test"], "digest_size": 15,
              "smtp_host": "smtp.invalid", "smtp_port": 587},
    "whatsapp": {"enabled": False, "tiers": ["HOT", "URGENT"]},
}}


def deal(pid, price, was, **extra):
    return {**scored(price, was, **extra), "product_id": pid}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        # Replace the real senders with fakes that just record their calls.
        self.ntfy = mock.patch.object(notify.channels, "send_ntfy").start()
        self.email = mock.patch.object(notify.channels, "send_email").start()
        self.addCleanup(mock.patch.stopall)
        self.notifier = notify.Notifier(CONFIG)
        self.in_stock = lambda url: {"price": 0.0, "is_sold_out": False}

    def test_urgent_goes_to_push_and_email_once(self):
        deals = [deal(1, 0, 260)]
        sent = self.notifier.alert_new_deals(self.conn, deals, confirm=self.in_stock)
        self.assertEqual(sent["URGENT"], 1)
        self.assertEqual(self.ntfy.call_count, 1)
        self.assertEqual(self.email.call_count, 1)
        self.assertEqual(self.ntfy.call_args.kwargs["priority"], 5)
        # Second run at the same price: nothing new is sent.
        sent = self.notifier.alert_new_deals(self.conn, deals, confirm=self.in_stock)
        self.assertEqual(sent["URGENT"], 0)
        self.assertEqual(self.ntfy.call_count, 1)

    def test_sold_out_on_product_page_is_skipped_and_not_rechecked(self):
        sold_out = mock.Mock(return_value={"price": 0.0, "is_sold_out": True})
        sent = self.notifier.alert_new_deals(self.conn, [deal(1, 0, 260)], confirm=sold_out)
        self.assertEqual(sent["skipped"], 1)
        self.ntfy.assert_not_called()
        self.notifier.alert_new_deals(self.conn, [deal(1, 0, 260)], confirm=sold_out)
        self.assertEqual(sold_out.call_count, 1)  # not checked again at the same price

    def test_hot_deals_are_batched_above_threshold(self):
        hot = [deal(i, 100, 1000) for i in range(5)]
        sent = self.notifier.alert_new_deals(self.conn, hot)
        self.assertEqual(sent["HOT"], 5)
        self.assertEqual(self.ntfy.call_count, 1)  # one summary push, not five
        self.email.assert_not_called()             # HOT deals don't email

    def test_normal_and_sold_out_deals_are_ignored(self):
        self.notifier.alert_new_deals(self.conn, [deal(1, 900, 1000), deal(2, 0, 260, is_sold_out=True)])
        self.ntfy.assert_not_called()

    def test_not_recorded_when_no_channel_is_set_up(self):
        self.ntfy.side_effect = notify.NotConfigured("NTFY_TOPIC is not set")
        self.email.side_effect = notify.NotConfigured("SMTP_USER is not set")
        self.notifier.alert_new_deals(self.conn, [deal(1, 0, 260)], confirm=self.in_stock)
        # Once you add your secrets, you should still get this alert.
        self.assertEqual(len(storage.not_yet_alerted(self.conn, [deal(1, 0, 260)])), 1)

    def test_digest_email_has_a_table(self):
        self.notifier.send_digest([deal(1, 0, 260), deal(2, 100, 1000)])
        subject, text, html = self.email.call_args.args[1:]
        self.assertIn("top 2 deals", subject)
        self.assertIn("<table", html)


if __name__ == "__main__":
    unittest.main()
