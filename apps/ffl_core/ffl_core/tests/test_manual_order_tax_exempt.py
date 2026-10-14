"""Tax-exempt Create Order — the Sales Invoice must stay at ZERO tax
end-to-end, mirroring the Sales Order it's derived from.

Bug (2026-07-09, found on a past invoice): a tax-exempt
buyer's create_order correctly skipped _apply_default_tax_template on the
Sales Order AND the invoice's first pass, but paying in full inline (the
is_pos path staff use for a normal counter sale) reran si.set_missing_values()
a second time — its set_pos_fields, now that is_pos=1, unconditionally
overwrote taxes_and_charges from the POS Profile's default template. The
invoice came back 8.25% taxed on an exempt sale. `exempt_from_sales_tax` has
no server-side consumer in this system and can't be relied on; the only
correct signal is an empty taxes_and_charges + an empty taxes table, exactly
like the Sales Order.

Fixed by _reassert_tax_exemption(si, tax_exempt), called right after every
set_missing_values() pass (mirroring how _repin_economics re-pins rate/
discount/margin for the same "set_missing_values wiped it" reason).

This module needs a REAL company-default Sales Taxes and Charges Template
(the dev site has none by default) to reproduce the bug at all — without one,
the POS Profile's default-template refill is a no-op and the exemption holds
by accident. Mirrors test_interstate_tax.py's fixture (same template title,
idempotent) so whichever test module runs first in `make test` wins."""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin


class TaxExemptOrderFixtures(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin (not BuildOrderFixtures) on purpose — the fixture CLASS
	carries commit-heavy test methods unittest would re-run per subclass (see
	test_build_order.py's own docstring on this)."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls._ensure_default_tax_template()
		cls._ensure_pos_profile_default_tax()

	@classmethod
	def _ensure_default_tax_template(cls) -> None:
		"""A company-default (is_default=1) 8.25% Sales Taxes and Charges
		Template — the real prod shape this bug needs to reproduce. Same
		title/company test_interstate_tax.py uses, created idempotently so
		either test module may run first in the same `bench run-tests --app
		ffl_core` session without colliding."""
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls.default_template = f"Texas Sales Tax - {abbr}"
		if frappe.db.exists("Sales Taxes and Charges Template", cls.default_template):
			return
		acct_name = frappe.db.get_value(
			"Account", {"company": cls.company, "account_name": "ITX Sales Tax"}, "name")
		if not acct_name:
			parent = frappe.db.get_value(
				"Account", {"company": cls.company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			acct = frappe.get_doc({
				"doctype": "Account", "account_name": "ITX Sales Tax",
				"company": cls.company, "parent_account": parent,
				"account_type": "Tax",
			})
			acct.insert(ignore_permissions=True)
			acct_name = acct.name
		frappe.get_doc({
			"doctype": "Sales Taxes and Charges Template",
			"title": "Texas Sales Tax", "company": cls.company, "is_default": 1,
			"taxes": [{"charge_type": "On Net Total", "account_head": acct_name,
				"rate": 8.25, "description": "TX Sales Tax"}],
		}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_pos_profile_default_tax(cls) -> None:
		"""THE prod-parity piece: a real counter POS Profile carries its own
		default `taxes_and_charges` (staff configure it once so a normal
		walk-in sale taxes correctly without picking a template by hand). The
		dev-site fixture (setup.dev_site._pos_profile) doesn't set this, so
		without it set_pos_fields' POS-Profile refill has nothing to
		reintroduce and the bug is invisible — this is the piece that makes
		the failing test actually fail pre-fix."""
		pos = frappe.db.get_value(
			"POS Profile", {"company": cls.company, "disabled": 0}, "name")
		if pos and frappe.db.get_value("POS Profile", pos, "taxes_and_charges") != cls.default_template:
			frappe.db.set_value("POS Profile", pos, "taxes_and_charges",
				cls.default_template, update_modified=False)

	def _pos_profile(self) -> str:
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.assertTrue(pos, "dev_site should seed a POS Profile for the test company")
		return pos

	def _edit(self, si, mutate=None, **kw):
		out = manual_order.get_order_for_edit(si.name)
		payload = out["payload"]
		if mutate:
			mutate(payload)
		return manual_order.update_order(si.name, payload, **kw)


# ------------------------------------------------------- create_order, paid


class PaidShipNowTax(TaxExemptOrderFixtures):
	def test_tax_exempt_paid_ship_now_has_zero_tax(self):
		"""THE prod bug: exempt + paid-in-full (is_pos) + ship-now must NOT
		come back taxed."""
		serial = self._stocked_serial("TXE-PAID-1")
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("TXE Paid Buyer"),
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 1, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1900, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.is_pos, 1)  # confirms the is_pos re-tax path ran
		self.assertFalse(si.taxes_and_charges)
		self.assertEqual(len(si.taxes), 0)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 0, places=2)
		self.assertAlmostEqual(flt(si.grand_total), flt(si.net_total), places=2)
		self.assertAlmostEqual(flt(si.grand_total), 1900, places=2)

	def test_non_exempt_paid_ship_now_still_gets_default_tax(self):
		"""Regression guard: a normal (non-exempt) paid counter sale must keep
		being taxed at the company default — the fix must not blanket-zero tax
		for everyone."""
		serial = self._stocked_serial("TXE-PAID-2")
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("TXE Taxed Buyer"),
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 1, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1000, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		self.assertEqual(si.taxes_and_charges, self.default_template)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 1000 * 0.0825, places=2)
		self.assertGreater(flt(si.grand_total), flt(si.net_total))


# --------------------------------------------------- create_order, deferred


class DeferredTax(TaxExemptOrderFixtures):
	def test_tax_exempt_deferred_unpaid_has_zero_tax(self):
		"""The non-is_pos path (billing-only, no inline payment) only runs
		set_missing_values() once — guards the insert-time call site
		independently of the is_pos re-tax path above."""
		serial = self._stocked_serial("TXE-DEFER-1")
		out = manual_order.create_order({
			"buyer_type": "business", "customer": "_Test BO Dealer",
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 0, "payment_received": 0, "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1200, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertFalse(si.is_pos)
		self.assertFalse(si.taxes_and_charges)
		self.assertEqual(len(si.taxes), 0)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 0, places=2)

	def test_non_exempt_deferred_unpaid_still_gets_default_tax(self):
		serial = self._stocked_serial("TXE-DEFER-2")
		out = manual_order.create_order({
			"buyer_type": "business", "customer": "_Test BO Dealer",
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 0, "payment_received": 0, "tax_exempt": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1200, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.taxes_and_charges, self.default_template)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 1200 * 0.0825, places=2)


# ----------------------------------------------------------- edit / rebuild


class EditRebuildTax(TaxExemptOrderFixtures):
	def test_edit_rebuild_keeps_tax_exempt_at_zero(self):
		"""update_order cancels + rebuilds THROUGH create_order — same
		function, so the fix must survive an edit. Build the original paid
		in full (is_pos) + deferred (so it's still editable — a booked
		disposition would block the edit), then edit it and confirm the
		REBUILT invoice is still untaxed."""
		serial = self._stocked_serial("TXE-EDIT-1")
		customer = self._indiv("TXE Edit Buyer")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 0, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 800, "serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 0, places=2)

		# Mutate notes, not shipping_amount — a shipping charge rides the SAME
		# `taxes` child table (compliance.shipping_charge) as an unrelated
		# "Actual" row, which would muddy a len(taxes) assertion here.
		out = self._edit(si, mutate=lambda p: p.__setitem__(
			"notes", "Edited — still exempt."))
		self.assertTrue(out["ok"])
		new_si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertFalse(new_si.taxes_and_charges)
		self.assertEqual(len(new_si.taxes), 0)
		self.assertAlmostEqual(flt(new_si.total_taxes_and_charges), 0, places=2)
		self.assertAlmostEqual(flt(new_si.grand_total), flt(new_si.net_total), places=2)

	def test_edit_rebuild_non_exempt_keeps_default_tax(self):
		"""Regression guard for the edit path: a normal taxed order must stay
		taxed after an edit — the customer's standing flag (not the payload)
		drives the rebuild's exemption decision (get_order_for_edit omits
		tax_exempt on purpose)."""
		serial = self._stocked_serial("TXE-EDIT-2")
		customer = self._indiv("TXE Edit Taxed Buyer")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 0, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 800, "serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		self.assertGreater(flt(si.total_taxes_and_charges), 0)

		out = self._edit(si, mutate=lambda p: p.__setitem__(
			"notes", "Edited — still taxed."))
		new_si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(new_si.taxes_and_charges, self.default_template)
		self.assertGreater(flt(new_si.total_taxes_and_charges), 0)


# ------------------------------------ hook-specific coverage


class FreightSurvivesExemption(TaxExemptOrderFixtures):
	"""compliance.customer_tax_exempt.clear_tax_keep_freight's ONLY non-trivial
	decision: an exempt order still bills its shipping "Actual" charge row —
	freight isn't sales tax. None of the other 6 tests exercise this (the edit
	test deliberately avoids shipping_amount to keep its len(taxes) assertion
	simple), so a regression here (e.g. a typo'd account_head match, or
	shipping_account resolving None) would silently zero shipping on every
	exempt order and go undetected."""

	def test_tax_exempt_paid_ship_now_with_shipping_keeps_freight_only(self):
		serial = self._stocked_serial("TXE-FREIGHT-1")
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("TXE Freight Buyer"),
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 1, "payment_received": 1, "mode_of_payment": "Cash",
			"tax_exempt": 1, "shipping_amount": 35,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1000, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertFalse(si.taxes_and_charges)  # no sales-tax template survives
		self.assertEqual(len(si.taxes), 1)  # exactly the freight row — no sales tax rows
		self.assertEqual(si.taxes[0].charge_type, "Actual")
		self.assertAlmostEqual(flt(si.taxes[0].tax_amount), 35, places=2)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 35, places=2)
		self.assertAlmostEqual(flt(si.grand_total), flt(si.net_total) + 35, places=2)


class ReturnInvoiceSkipped(TaxExemptOrderFixtures):
	"""A credit-note (is_return) Sales Invoice must never be touched by this
	hook — a return's tax follows the ORIGINAL sale being reversed, regardless
	of the customer's current exemption flag. Direct function-level call
	(mirrors test_interstate_tax.py's guard_pos_exemption tests): the hook's
	is_return check must short-circuit before it ever reads Customer.is_tax_exempt
	or mutates anything, so this is real even for a customer who IS exempt."""

	def test_is_return_invoice_hook_never_mutates(self):
		from types import SimpleNamespace

		from ffl_core.compliance.customer_tax_exempt import enforce_customer_tax_exemption

		customer = self._indiv("TXE Return Buyer")
		manual_order.set_customer_tax_exempt(customer, 1)
		original_taxes = [{"account_head": "Some Tax Account", "tax_amount": 82.5}]
		doc = SimpleNamespace(
			is_return=1, is_consolidated=0, woo_online_order=None,
			customer=customer, company=self.company,
			taxes_and_charges=self.default_template, taxes=list(original_taxes),
		)
		enforce_customer_tax_exemption(doc)
		self.assertEqual(doc.taxes_and_charges, self.default_template)
		self.assertEqual(doc.taxes, original_taxes)


class HookAloneStripsDirectlyBuiltInvoice(TaxExemptOrderFixtures):
	"""Proves enforce_customer_tax_exemption is independently load-bearing —
	not just riding along with manual_order._reassert_tax_exemption. Builds a
	Sales Invoice the way test_interstate_tax.py's _so() does (bypassing
	manual_order.create_order entirely, and pre-seeding taxes_and_charges +
	taxes explicitly since set_missing_values short-circuits under
	frappe.in_test), then relies SOLELY on the validate-time hook to strip it."""

	def test_hook_strips_default_tax_on_directly_built_invoice(self):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		customer = self._indiv("TXE Direct Buyer")
		manual_order.set_customer_tax_exempt(customer, 1)
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = customer
		si.posting_date = frappe.utils.nowdate()
		si.due_date = frappe.utils.nowdate()
		si.append("items", {"item_code": "BO-ACC", "qty": 1, "rate": 100})
		si.taxes_and_charges = self.default_template
		si.set("taxes", get_taxes_and_charges(
			"Sales Taxes and Charges Template", self.default_template))
		si.flags.ignore_permissions = True
		si.insert()
		self.assertFalse(si.taxes_and_charges)
		self.assertEqual(len(si.taxes), 0)
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 0, places=2)
		self.assertAlmostEqual(flt(si.grand_total), flt(si.net_total), places=2)


if __name__ == "__main__":
	unittest.main()
