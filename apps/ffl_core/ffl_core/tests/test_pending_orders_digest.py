"""The daily stuck-orders digest: nothing stuck → no mail;
stuck → one mail, deduped per day. Site test."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order
from ffl_core.patches import seed_pending_order_age_alert as patch
from ffl_core.utils import single_raw


class Digest(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.cache().delete_value(manual_order._DIGEST_CACHE_PREFIX + frappe.utils.nowdate())

	def test_nothing_stuck_sends_nothing(self):
		with mock.patch.object(manual_order, "list_pending_dispositions", return_value=[]), \
				mock.patch.object(frappe, "sendmail") as mail:
			out = manual_order.pending_orders_digest()
		self.assertEqual(out, {"lines": 0, "sent": False})
		mail.assert_not_called()

	def test_stuck_rows_send_one_mail_with_the_lines_then_stay_quiet_for_the_day(self):
		rows = [{"stage": "blocked", "blocked_reason": "Unpaid", "customer_name": "A",
			"sales_invoice": "SI-1", "age_days": 2}]
		with mock.patch.object(manual_order, "list_pending_dispositions", return_value=rows) as listing, \
				mock.patch.object(manual_order, "_digest_recipients", return_value=["m@example.com"]), \
				mock.patch.object(frappe, "sendmail") as mail:
			out = manual_order.pending_orders_digest()
			self.assertEqual(out, {"lines": 1, "sent": True})
			kwargs = mail.call_args.kwargs
			self.assertIn("Pending orders needing attention", kwargs["subject"])
			self.assertIn("SI-1", kwargs["message"])
			self.assertIn("Unpaid", kwargs["message"])
			self.assertEqual(kwargs["recipients"], ["m@example.com"])
			# Same day again: the queue is not even read.
			again = manual_order.pending_orders_digest()
			self.assertEqual(again, {"lines": 0, "sent": False})
			self.assertEqual(listing.call_count, 1)
			self.assertEqual(mail.call_count, 1)

	def test_no_recipients_means_nothing_sent_but_lines_counted(self):
		rows = [{"stage": "blocked", "blocked_reason": "Unpaid", "customer_name": "A",
			"sales_invoice": "SI-1", "age_days": 2}]
		with mock.patch.object(manual_order, "list_pending_dispositions", return_value=rows), \
				mock.patch.object(manual_order, "_digest_recipients", return_value=[]), \
				mock.patch.object(frappe, "sendmail") as mail:
			out = manual_order.pending_orders_digest()
		self.assertEqual(out, {"lines": 1, "sent": False})
		mail.assert_not_called()

	def test_threshold_patch_fills_only_a_blank_row(self):
		frappe.db.delete("Singles", {"doctype": "FFL Settings", "field": "pending_order_age_alert_days"})
		patch.execute()
		self.assertEqual(int(single_raw("FFL Settings", "pending_order_age_alert_days")), 7)
		frappe.db.set_single_value("FFL Settings", "pending_order_age_alert_days", 3)
		patch.execute()
		self.assertEqual(int(single_raw("FFL Settings", "pending_order_age_alert_days")), 3)
