"""Tests for the customer NICS-cleared pickup email."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.customer_notify import notify_customer_nics_cleared


class TestNicsClearedNotify(FrappeTestCase):
	def _inv(self, **over):
		base = dict(customer="C1", customer_name="Buyer",
			contact_email="buyer@example.com", custom_nics_cleared_notified=0)
		base.update(over)
		return frappe._dict(base)

	@patch("frappe.db.set_value")
	@patch("frappe.sendmail")
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value")
	def test_sends_and_sets_flag(self, mock_gv, mock_get_doc, mock_sendmail, mock_sv):
		mock_gv.return_value = self._inv()
		mock_get_doc.return_value = frappe._dict(
			subject="Cleared {{ invoice }}", use_html=1,
			response_html="<p>Hi {{ customer_name }}</p>", response="Hi")
		notify_customer_nics_cleared("POS-1")
		mock_sendmail.assert_called_once()
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["buyer@example.com"])
		mock_sv.assert_called_once_with(
			"POS Invoice", "POS-1", "custom_nics_cleared_notified", 1, update_modified=False)

	@patch("frappe.sendmail")
	@patch("frappe.db.get_value")
	def test_idempotent_when_already_notified(self, mock_gv, mock_sendmail):
		mock_gv.return_value = self._inv(custom_nics_cleared_notified=1)
		notify_customer_nics_cleared("POS-1")
		mock_sendmail.assert_not_called()

	@patch("frappe.sendmail")
	@patch("frappe.db.get_value")
	def test_no_email_no_send(self, mock_gv, mock_sendmail):
		# 1st get_value → invoice dict w/o contact_email; 2nd → Customer.email_id empty
		mock_gv.side_effect = [self._inv(contact_email=""), ""]
		notify_customer_nics_cleared("POS-1")
		mock_sendmail.assert_not_called()
