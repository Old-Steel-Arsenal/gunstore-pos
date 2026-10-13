"""A caller that pins a document's tax decision must still have it after
`set_missing_values()` — the template AND its rows.

ERPNext's `set_pos_fields(for_validate=False)` copies a fixed list of fields
off the POS Profile with no regard for what the document already carries::

    for fieldname in ("currency", …, "taxes_and_charges", …):
        if (not for_validate) or (for_validate and not self.get(fieldname)):
            self.set(fieldname, pos.get(fieldname))

so on the `for_validate=False` pass — the one `set_missing_values()` makes —
the profile wins unconditionally, including when the profile's value is empty.
Then, at the end of the same method, it fills an empty `taxes` table from the
template it has just written over ours:

    # fetch charges
    if self.taxes_and_charges and not len(self.get("taxes")):
        self.set_taxes()

So a script that assigns a tax decision and then calls `set_missing_values()`
loses it silently: no error, no warning. Restoring only the FIELD is worse than
restoring neither — the document then names one template while carrying
another's rows, and `compliance.interstate_tax.apply_interstate_tax` decides
whether to rebuild the rows by looking at the NAME, so it sees the name it
wanted and leaves the wrong rows alone. That is an out-of-state order billed
Texas sales tax.

Both directions bite a real caller here:

* `api/manual_order.create_order` pins the company default template, then the
  paid-in-full (is_pos) path reruns `set_missing_values()`. On a POS Profile
  with no template of its own — which is what `setup/dev_site` builds and what
  any store gets before someone configures the register — the counter sale's
  template is wiped.
* `ffl_woo_sync/woocommerce/pickup` pins it EMPTY on purpose, so that a later
  POS-UI touch cannot pull the profile's template in and recompute tax over
  the figure WooCommerce actually charged the buyer. `store_setup` configures
  a real store's profile with a template, so that pin was being overwritten by
  it every time.

The fix is `ffl_core.utils.set_missing_values_keeping_taxes`, which puts back
both halves together. ERPNext core is upgrade-safe territory and is not
patched; the caller re-asserts what it owns, exactly as
`manual_order._repin_economics` re-pins rate and discount after the same call
for the same reason.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.interstate_tax import OUT_OF_STATE_TEMPLATE_TITLE

ITEM = "TAXTMPL-ACC"
CUSTOMER = "_Test Tax Template Buyer"


class TaxesTemplateFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.pos_profile = frappe.db.get_value(
			"POS Profile", {"company": cls.company, "disabled": 0}, "name")
		cls.template = cls._ensure_default_tax_template()
		cls.oos_template = cls._ensure_oos_template()

		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_default_tax_template(cls) -> str:
		"""Same title/company as test_interstate_tax.py and
		test_manual_order_tax_exempt.py use, created idempotently so whichever
		module runs first in a shared `bench run-tests --app ffl_core` wins."""
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

	def _profile_template(self, value: str | None) -> None:
		"""Pin the POS Profile's own template for this test, and put it back.

		Restored explicitly rather than left to the transaction: a commit
		landing mid-run would otherwise make it permanent, and the neighbouring
		tax modules read this exact field."""
		before = frappe.db.get_value("POS Profile", self.pos_profile, "taxes_and_charges")
		self.addCleanup(frappe.db.set_value, "POS Profile", self.pos_profile,
			"taxes_and_charges", before, update_modified=False)
		frappe.db.set_value("POS Profile", self.pos_profile, "taxes_and_charges",
			value, update_modified=False)

	@classmethod
	def _ensure_oos_template(cls) -> str:
		"""The 0% out-of-state template compliance/interstate_tax.py owns — no
		rows at all, which is what makes the empty taxes table meaningful."""
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		name = f"{OUT_OF_STATE_TEMPLATE_TITLE} - {abbr}"
		if not frappe.db.exists("Sales Taxes and Charges Template", name):
			frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": OUT_OF_STATE_TEMPLATE_TITLE, "company": cls.company,
				"is_default": 0, "disabled": 0, "taxes": [],
			}).insert(ignore_permissions=True)
		return name

	def _counter_sales_invoice(self, taxes_and_charges):
		"""The shape create_order hands to its is_pos rerun: a Sales Invoice
		with is_pos and the register's profile already set."""
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = CUSTOMER
		si.is_pos = 1
		si.pos_profile = self.pos_profile
		si.append("items", {"item_code": ITEM, "qty": 1, "rate": 100,
			"price_list_rate": 100})
		si.taxes_and_charges = taxes_and_charges
		return si

	def _pos_invoice(self, taxes_and_charges):
		"""An unsaved POS Invoice — the defect is entirely inside
		set_missing_values(), so nothing here needs an open shift or stock."""
		inv = frappe.new_doc("POS Invoice")
		inv.company = self.company
		inv.pos_profile = self.pos_profile
		inv.customer = CUSTOMER
		inv.append("items", {"item_code": ITEM, "qty": 1, "rate": 100,
			"price_list_rate": 100})
		inv.taxes_and_charges = taxes_and_charges
		return inv


class CoreClobbersTheCallersTemplate(TaxesTemplateFixtures):
	"""Characterisation of the ERPNext behaviour being worked around. If an
	upgrade ever makes these fail, the workaround below can go."""

	def test_an_empty_profile_wipes_a_caller_set_template(self):
		self._profile_template(None)
		inv = self._pos_invoice(self.template)
		inv.set_missing_values()
		self.assertFalse(inv.taxes_and_charges,
			"upstream stopped clobbering — re-check whether the helper is still needed")

	def test_a_profile_template_overwrites_a_deliberately_blank_one(self):
		self._profile_template(self.template)
		inv = self._pos_invoice("")
		inv.set_missing_values()
		self.assertEqual(inv.taxes_and_charges, self.template,
			"upstream stopped clobbering — re-check whether the helper is still needed")

	def test_an_empty_taxes_table_is_refilled_from_the_profile_template(self):
		"""The second half, and the one that makes restoring the field ALONE
		actively harmful: set_pos_fields writes the profile's template over the
		document's and then, at the end of the same method, fills the empty
		taxes table from THAT template. A document left naming one template
		while carrying another's rows is worse than one that lost both.

		Shaped exactly like create_order's is_pos rerun — a Sales Invoice with
		is_pos already set — because that is where it bites."""
		self._profile_template(self.template)
		si = self._counter_sales_invoice(taxes_and_charges=self.oos_template)
		self.assertEqual(len(si.get("taxes") or []), 0, "fixture precondition")
		si.set_missing_values()
		self.assertEqual(si.taxes_and_charges, self.template)
		self.assertGreater(
			len(si.get("taxes") or []), 0,
			"upstream stopped refilling — re-check whether the helper is still needed")


class HelperKeepsTheCallersTemplate(TaxesTemplateFixtures):
	def test_a_caller_set_template_survives(self):
		"""manual_order.create_order's case: the company default is pinned
		before the call and must still be there after it."""
		from ffl_core.utils import set_missing_values_keeping_taxes

		self._profile_template(None)
		inv = self._pos_invoice(self.template)
		set_missing_values_keeping_taxes(inv)
		self.assertEqual(inv.taxes_and_charges, self.template)

	def test_a_deliberately_blank_template_survives(self):
		"""pickup.py's case, and the reason "restore only when it was blanked"
		is not good enough: an empty pin is a decision too, and the profile's
		template must not be allowed to overwrite it."""
		from ffl_core.utils import set_missing_values_keeping_taxes

		self._profile_template(self.template)
		inv = self._pos_invoice("")
		set_missing_values_keeping_taxes(inv)
		self.assertFalse(inv.taxes_and_charges)

	def test_everything_else_is_still_filled_in(self):
		"""The helper protects the tax decision and nothing else. Turning
		set_missing_values() into a no-op would break every caller far more
		quietly than the bug it fixes, so pin something else the call is
		responsible for."""
		from ffl_core.utils import set_missing_values_keeping_taxes

		self._profile_template(None)
		inv = self._pos_invoice(self.template)
		self.assertFalse(inv.get("debit_to"), "fixture precondition")
		set_missing_values_keeping_taxes(inv)
		self.assertTrue(inv.debit_to)

	def test_an_out_of_state_order_keeps_its_empty_taxes_table(self):
		"""The regression a field-only restore caused, and the reason both
		halves move together: an out-of-state order carries a 0% template with
		NO rows. Putting the template back while letting the profile's template
		fill the rows produced an invoice that NAMED the exempt template and
		CHARGED 8.25% — and apply_interstate_tax, which decides whether to
		repair by comparing the NAME, saw the name it wanted and left the wrong
		rows alone."""
		from ffl_core.utils import set_missing_values_keeping_taxes

		self._profile_template(self.template)
		si = self._counter_sales_invoice(taxes_and_charges=self.oos_template)
		set_missing_values_keeping_taxes(si)
		self.assertEqual(si.taxes_and_charges, self.oos_template)
		self.assertEqual(len(si.get("taxes") or []), 0)

	def test_caller_built_rows_are_kept_verbatim(self):
		"""pickup.py's case: the rows carry WooCommerce's own figures, which
		must not be recomputed or replaced by anything derived from a
		template."""
		from ffl_core.utils import set_missing_values_keeping_taxes

		self._profile_template(self.template)
		inv = self._pos_invoice("")
		account = frappe.db.get_value(
			"Sales Taxes and Charges", {"parent": self.template}, "account_head")
		inv.append("taxes", {"charge_type": "Actual", "account_head": account,
			"description": "Sales tax collected by WooCommerce", "tax_amount": 7.31})
		set_missing_values_keeping_taxes(inv)
		self.assertEqual(len(inv.taxes), 1)
		self.assertEqual(inv.taxes[0].charge_type, "Actual")
		self.assertAlmostEqual(inv.taxes[0].tax_amount, 7.31, places=2)

	def test_a_sales_invoice_is_handled_too(self):
		"""The same core method runs on Sales Invoice (the counter order's
		document), so the helper must not be POS-Invoice-shaped."""
		from ffl_core.utils import set_missing_values_keeping_taxes

		self._profile_template(None)
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = CUSTOMER
		si.is_pos = 1
		si.pos_profile = self.pos_profile
		si.append("items", {"item_code": ITEM, "qty": 1, "rate": 100,
			"price_list_rate": 100})
		si.taxes_and_charges = self.template
		set_missing_values_keeping_taxes(si)
		self.assertEqual(si.taxes_and_charges, self.template)


if __name__ == "__main__":
	unittest.main()
