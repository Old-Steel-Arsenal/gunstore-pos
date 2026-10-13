"""A counter order paid in full keeps the tax template it was built with.

Sibling of test_manual_order_tax_exempt.py, from the other side. That module
pins its POS Profile's own ``taxes_and_charges`` to the company default —
prod parity for a configured register — and so the is_pos rerun of
``set_missing_values()`` happens to overwrite the invoice's template with an
identical value and nothing shows.

A register whose profile has NO template of its own is the other real shape:
``setup/dev_site`` builds one, and so does every store before somebody
configures the POS Profile. There, ``set_pos_fields``' unconditional copy
writes the profile's EMPTY value over the template ``_apply_default_tax_template``
pinned, and the invoice comes back naming no template at all while still
carrying the tax rows that template produced.

The money is right either way — nothing rebuilds the ``taxes`` rows server-side
— so this is not a mis-billing. It is an invoice that no longer says what it was
taxed under, on the document the store bills from, and it silently disarms
``compliance.interstate_tax.apply_interstate_tax``'s corrective branch, which
can only revert OUR out-of-state template when it can see it named.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin


class UntemplatedProfileFixtures(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin, not BuildOrderFixtures — the fixture CLASS carries
	commit-heavy test methods unittest would re-run per subclass."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.default_template = cls._ensure_default_tax_template()

	@classmethod
	def _ensure_default_tax_template(cls) -> str:
		"""Same title/company as test_interstate_tax.py and
		test_manual_order_tax_exempt.py, created idempotently so whichever
		module runs first in a shared session wins."""
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		name = f"Texas Sales Tax - {abbr}"
		if frappe.db.exists("Sales Taxes and Charges Template", name):
			return name
		account = frappe.db.get_value(
			"Account", {"company": cls.company, "account_name": "ITX Sales Tax"}, "name")
		if not account:
			parent = frappe.db.get_value(
				"Account", {"company": cls.company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			acct = frappe.get_doc({
				"doctype": "Account", "account_name": "ITX Sales Tax",
				"company": cls.company, "parent_account": parent,
				"account_type": "Tax",
			})
			acct.insert(ignore_permissions=True)
			account = acct.name
		frappe.get_doc({
			"doctype": "Sales Taxes and Charges Template",
			"title": "Texas Sales Tax", "company": cls.company, "is_default": 1,
			"taxes": [{"charge_type": "On Net Total", "account_head": account,
				"rate": 8.25, "description": "TX Sales Tax"}],
		}).insert(ignore_permissions=True)
		return name

	def setUp(self) -> None:
		super().setUp()
		self.pos_profile = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.assertTrue(self.pos_profile, "dev_site should seed a POS Profile")
		# An UNCONFIGURED register: no template of its own. Restored explicitly
		# rather than left to the transaction — create_order's submit chain
		# commits, and the neighbouring tax modules read this exact field.
		before = frappe.db.get_value("POS Profile", self.pos_profile, "taxes_and_charges")
		self.addCleanup(frappe.db.set_value, "POS Profile", self.pos_profile,
			"taxes_and_charges", before, update_modified=False)
		frappe.db.set_value("POS Profile", self.pos_profile, "taxes_and_charges",
			None, update_modified=False)


class PaidCounterOrderKeepsItsTemplate(UntemplatedProfileFixtures):
	def test_paid_in_full_on_an_untemplated_register_keeps_the_default_template(self):
		"""The defect: the is_pos rerun of set_missing_values() wrote the
		profile's empty taxes_and_charges over the company default."""
		serial = self._stocked_serial("TXTMPL-PAID-1")
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("TXTMPL Paid Buyer"),
			"pos_profile": self.pos_profile, "destination_ffl": DEST_FFL,
			"dispose_now": 1, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1000,
				"serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.is_pos, 1, "confirms the is_pos rerun path ran")
		self.assertEqual(si.taxes_and_charges, self.default_template)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 1000 * 0.0825, places=2)

	def test_an_exempt_order_on_an_untemplated_register_is_still_untaxed(self):
		"""The guard in the other direction: keeping the caller's value must
		not resurrect a template on an order that deliberately has none."""
		serial = self._stocked_serial("TXTMPL-PAID-2")
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("TXTMPL Exempt Buyer"),
			"pos_profile": self.pos_profile, "destination_ffl": DEST_FFL,
			"dispose_now": 1, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1000,
				"serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		self.assertFalse(si.taxes_and_charges)
		self.assertEqual(len(si.taxes), 0)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 0, places=2)


if __name__ == "__main__":
	unittest.main()
