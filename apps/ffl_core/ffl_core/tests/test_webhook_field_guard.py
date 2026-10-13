"""Server-side protection of the four
webhook-owned 4473 fields on POS Invoice against a stale-client full-doc save.

The FastBound webhook stamps form_4473_status / nics_status /
fastbound_disposition_id / fastbound_4473_pdf_url with db.set_value(
update_modified=False), so Frappe's modified-timestamp conflict check never
fires. A cashier's browser that loaded the draft BEFORE the webhook still holds
the pre-completion values; its next full-doc save (e.g. editing the payment
split) writes those stale values back and reverts the order to Pending. The
version history of the affected invoice is the proof.

The guard restores the four fields (as a group) from the DB on any Draft POS
Invoice save that would regress them, unless an explicit server-side reset opts
out via doc.flags.ignore_webhook_4473_guard.

Run inside the bench container:
    bench --site dev.localhost run-tests --module ffl_core.tests.test_webhook_field_guard
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase


# The real incident's disposition id + a representative PDF url.
DISP_ID = "65745905-a232-4a98-a094-b483010c0587"
PDF_URL = "https://cloud.fastbound.com/TEST-ACC/api/Form4473s/61fc50d7/download"


class WebhookFieldGuard(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.customer = "_Test WFG Buyer"
		if not frappe.db.exists("Customer", cls.customer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cls.customer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company)
		if not frappe.db.exists("Item", "WFG-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "WFG-GUN", "item_name": "WFG-GUN",
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
				"serial_no_series": "WFG-GUN-.####",
			}).insert(ignore_permissions=True)

	def _draft(self, serial: str, *, form_4473: str = "Pending", nics: str = "N/A") -> str:
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = self.customer
		inv.append("items", {"item_code": "WFG-GUN", "qty": 1, "rate": 600,
			"price_list_rate": 600, "serial_no": serial,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1})
		inv.set_missing_values()
		inv.form_4473_status = form_4473
		inv.nics_status = nics
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _completed_draft(self, serial: str) -> str:
		"""A held firearm draft the webhook has completed — exactly the webhook's
		write path: db.set_value(update_modified=False) of the four fields."""
		name = self._draft(serial)
		frappe.db.set_value("POS Invoice", name, {
			"form_4473_status": "Completed",
			"nics_status": "Proceed",
			"fastbound_disposition_id": DISP_ID,
			"fastbound_4473_pdf_url": PDF_URL,
		}, update_modified=False)
		return name

	def _four(self, name: str):
		return frappe.db.get_value("POS Invoice", name, [
			"form_4473_status", "nics_status",
			"fastbound_disposition_id", "fastbound_4473_pdf_url",
		], as_dict=True)

	# -----------------------------------------------------------------------

	def test_stale_client_save_cannot_regress_webhook_4473_fields(self):
		"""The core incident: a stale browser doc saves the four fields back to
		their pre-completion values. The guard must restore the DB truth."""
		name = self._completed_draft("WFG-S1")
		# A browser that loaded before the webhook holds the pre-completion group.
		doc = frappe.get_doc("POS Invoice", name)
		doc.form_4473_status = "Pending"
		doc.nics_status = "N/A"
		doc.fastbound_disposition_id = ""
		doc.fastbound_4473_pdf_url = ""
		doc.flags.ignore_permissions = True
		doc.save()

		db = self._four(name)
		self.assertEqual(db.form_4473_status, "Completed",
			"stale save must not revert form_4473_status")
		self.assertEqual(db.nics_status, "Proceed",
			"stale save must not revert nics_status")
		self.assertEqual(db.fastbound_disposition_id, DISP_ID,
			"stale save must not blank the disposition id")
		self.assertEqual(db.fastbound_4473_pdf_url, PDF_URL,
			"stale save must not blank the 4473 PDF url")

	def test_legit_server_reset_with_flag_bypasses_guard(self):
		"""A legitimate server-side reset (e.g. re-opening a 4473) opts out via
		doc.flags.ignore_webhook_4473_guard and IS allowed to reset the group."""
		name = self._completed_draft("WFG-S2")
		doc = frappe.get_doc("POS Invoice", name)
		doc.form_4473_status = "Pending"
		doc.nics_status = "N/A"
		doc.fastbound_disposition_id = ""
		doc.fastbound_4473_pdf_url = ""
		doc.flags.ignore_webhook_4473_guard = True
		doc.flags.ignore_permissions = True
		doc.save()

		db = self._four(name)
		self.assertEqual(db.form_4473_status, "Pending",
			"an explicitly-flagged reset must be honoured")
		self.assertEqual(db.nics_status, "N/A")
		self.assertEqual(db.fastbound_disposition_id or "", "")
		self.assertEqual(db.fastbound_4473_pdf_url or "", "")

	def test_completed_group_survives_a_fresh_client_resave(self):
		"""A non-stale client (loaded AFTER the webhook, so it carries the correct
		Completed group) re-saving the draft must keep the group intact and not
		raise — the guard is surgical, not a blanket freeze."""
		name = self._completed_draft("WFG-S3")
		doc = frappe.get_doc("POS Invoice", name)  # already Completed/Proceed in memory
		self.assertEqual(doc.form_4473_status, "Completed")  # sanity: fresh load
		doc.flags.ignore_permissions = True
		doc.save()  # a benign re-save (payment edit etc.) must not disturb the group

		db = self._four(name)
		self.assertEqual(db.form_4473_status, "Completed")
		self.assertEqual(db.nics_status, "Proceed")
		self.assertEqual(db.fastbound_disposition_id, DISP_ID)
		self.assertEqual(db.fastbound_4473_pdf_url, PDF_URL)

	def test_pre_completion_draft_save_is_not_touched(self):
		"""Before the webhook completes it, a Pending draft saves normally — the
		guard's anchor (DB form_4473_status == 'Completed') is false, so nothing
		is forced."""
		name = self._draft("WFG-S4", form_4473="Pending", nics="N/A")
		doc = frappe.get_doc("POS Invoice", name)
		doc.flags.ignore_permissions = True
		doc.save()  # must not raise
		db = self._four(name)
		self.assertEqual(db.form_4473_status, "Pending")
		self.assertEqual(db.nics_status, "N/A")


if __name__ == "__main__":
	unittest.main()
