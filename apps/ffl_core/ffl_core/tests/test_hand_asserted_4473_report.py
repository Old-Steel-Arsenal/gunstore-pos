"""Hand-Asserted 4473 Dispositions — the Invoice column must actually resolve.

The report is the standing review surface for every bound-book entry a human
vouched for instead of FastBound, so "which sale was this?" is the whole point
of reading it. It asked FFL Disposition for ``sales_invoice``; both writers
(``manager_override_4473`` and ``attest_paper_4473``, sharing
``_book_hand_asserted_4473``) stamp ``linked_pos_invoice`` and never touch
``sales_invoice``. Every row therefore rendered a blank Invoice cell in
production — an audit column that silently answers nothing is worse than no
column at all, because a reviewer reads the blank as "no invoice attached",
not "the report is asking for the wrong field".

Driven through the real whitelisted writers rather than hand-built disposition
rows: the bug was a disagreement between the writer and the reader, and a
fixture that invents its own field values cannot catch that.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox

ITEM = "HA4473-GUN"
CUSTOMER = "_Test Hand Asserted Buyer"
REASON = "FastBound was down; the 4473 was completed on their side."
REPORT = ("ffl_core.ffl_core.report.hand_asserted_4473_dispositions."
	"hand_asserted_4473_dispositions")


class HandAsserted4473Report(SettingsSandbox, FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.stores = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "warehouse_name": "Stores"}, "name")
		cls.pos_profile = frappe.db.get_value(
			"POS Profile", {"company": cls.company, "disabled": 0}, "name")

		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		# POSInvoice.validate calls validate_pos_opening_entry unconditionally —
		# on the draft insert too — so the profile needs an Open shift before
		# anything below can even be staged.
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company, cls.pos_profile)

	# ------------------------------------------------------------- fixtures

	def _draft_invoice(self, serial: str) -> str:
		"""A draft counter ticket for one firearm, with the serial on file —
		FFLDisposition._require_serial_belongs_to_item refuses to submit
		against a serial that does not exist."""
		from ffl_core.firearm import serialized_receipt_row
		if not frappe.db.exists("Serial No", serial):
			se = frappe.get_doc({
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [serialized_receipt_row(
					item_code=ITEM, qty=1, warehouse=self.stores, basic_rate=500,
					serial=serial)],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()

		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = CUSTOMER
		inv.append("items", {
			"item_code": ITEM, "qty": 1, "rate": 900, "price_list_rate": 900,
			"serial_no": serial, "use_serial_batch_fields": 1,
			"allow_zero_valuation_rate": 1,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _row_for(self, disposition: str) -> dict:
		from ffl_core.ffl_core.report.hand_asserted_4473_dispositions \
			.hand_asserted_4473_dispositions import execute
		_columns, rows = execute({})
		match = [r for r in rows if r["name"] == disposition]
		self.assertEqual(len(match), 1,
			f"{disposition} should appear exactly once in the report")
		return match[0]

	# ---------------------------------------------------------------- tests

	def test_manager_override_row_names_its_invoice(self):
		"""The defect, end to end: the writer stamps linked_pos_invoice, so the
		report's Invoice cell has to carry that invoice, not a blank."""
		from ffl_integrations.fastbound.pos_4473 import manager_override_4473

		invoice = self._draft_invoice("HA4473-OVR-1")
		with patch("ffl_integrations.fastbound.tasks."
				"enqueue_disposition_push_on_submit"):
			out = manager_override_4473(invoice, REASON)
		self.assertEqual(len(out["dispositions"]), 1)

		row = self._row_for(out["dispositions"][0])
		self.assertEqual(row["invoice"], invoice)

	def test_a_manager_override_rings_no_bell(self):
		"""The NICS bell (Run B) rings on FastBound's verdict only. The manager who
		overrides is standing at the counter already."""
		from ffl_integrations.fastbound.pos_4473 import manager_override_4473

		invoice = self._draft_invoice("HA4473-OVR-BELL")
		with patch("ffl_integrations.fastbound.tasks."
				"enqueue_disposition_push_on_submit"):
			manager_override_4473(invoice, REASON)
		self.assertFalse(frappe.db.exists("Notification Log",
			{"document_type": "POS Invoice", "document_name": invoice}))

	def test_paper_attestation_row_names_its_invoice_too(self):
		"""The second writer. Both go through _book_hand_asserted_4473, so both
		stamp the same field — the column must resolve for the counter's paper
		attestation exactly as it does for a manager override."""
		from ffl_integrations.fastbound.pos_4473 import attest_paper_4473

		invoice = self._draft_invoice("HA4473-PAPER-1")
		self.set_single("FFL Settings", "paper_4473_mode", 1)
		with patch("ffl_integrations.fastbound.tasks."
				"enqueue_disposition_push_on_submit"):
			out = attest_paper_4473(invoice)
		self.assertEqual(len(out["dispositions"]), 1)

		row = self._row_for(out["dispositions"][0])
		self.assertEqual(row["invoice"], invoice)

	def test_row_carries_who_and_why_beside_the_invoice(self):
		"""The rest of the audit payload, pinned alongside the fix: a reviewer
		needs WHO asserted and WHY next to the invoice or the row still answers
		nothing."""
		from ffl_integrations.fastbound.pos_4473 import manager_override_4473

		invoice = self._draft_invoice("HA4473-OVR-2")
		with patch("ffl_integrations.fastbound.tasks."
				"enqueue_disposition_push_on_submit"):
			out = manager_override_4473(invoice, REASON)

		row = self._row_for(out["dispositions"][0])
		self.assertEqual(row["invoice"], invoice)
		self.assertEqual(row["override_by"], frappe.session.user)
		self.assertEqual(row["override_reason"], REASON)
		self.assertEqual(row["disposition_type"], "Retail Sale")
		self.assertEqual(row["serial"], "HA4473-OVR-2")

	def test_the_invoice_column_is_declared(self):
		"""A row key nothing declares renders nowhere. Guards against fixing
		the query and leaving the column list pointed at the dead field."""
		from ffl_core.ffl_core.report.hand_asserted_4473_dispositions \
			.hand_asserted_4473_dispositions import execute
		columns, _rows = execute({})
		fieldnames = {c["fieldname"] for c in columns}
		self.assertIn("invoice", fieldnames)
		self.assertNotIn("sales_invoice", fieldnames,
			"the dead field must not survive as a second, always-blank column")


if __name__ == "__main__":
	unittest.main()
