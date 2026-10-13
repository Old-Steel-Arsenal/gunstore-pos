"""Consignment GL posting — post the consignor's payable + COGS at sale.

On submit of a POS/Sales Invoice that sold a consignment gun, post the
consignor's share (P·(1-c%)) to the GL as Dr COGS / Cr Consignment
Payable[party=consignor] via a Journal Entry — so GL profit reduces to the
commission the store actually earned, reconciling with the Sales Report
(test_sales_report.py's test_consignment_cost_is_net_minus_commission
computes the exact same number as this module's cost).

Three layers, cheapest first (mirrors test_consignment_capture.py's split):
  - ConsignorShare: pure-function tests for ffl_core.consignment.consignor_share.
  - ConsignmentSourceField: the new Journal Entry custom field ensure() adds.
  - PostConsignmentPayableDirect: compliance.consignment.post_consignment_payable
    called directly over lightweight docs (SimpleNamespace wrapping frappe._dict
    rows — see test_consignment_capture.py's FreezeConsignmentCommissionRows for
    why: dict.items() is a builtin, so the outer "doc" can't be dict-like, but
    _line_serials' serials_for_line(row) needs row.get(), so each row must be).
    Covers is_return / is_consolidated / non-consignment / no-consignor / zero-share
    skips, and the unresolvable-COGS-account/cost-center actionable error,
    cheaply, without a real submitted invoice.
  - PostConsignmentPayableInvoice: end-to-end through the real Sales Invoice
    on_submit hook (hooks.py) — proves the wiring itself, not just the bare
    function. Uses an ANTIQUE firearm Item (is_antique=1) so the sale sails
    through require_4473_and_nics_for_firearm_sale / require_shippable_ffl_dealer
    without a FastBound 4473 or an ATF FFL Record: both gates key off
    is_regulated_firearm_item, which excludes antiques (see
    tests/test_4473_nics_gating.py). Sales Invoice, not POS Invoice, per the
    task brief — POS Invoice submit additionally requires a committed FFL
    Disposition per serial (_require_disposition_coverage), which only fires
    for doctype=="POS Invoice". Its last test (B3,
    test_sales_report_cost_reconciles_with_gl_cogs) reuses this same class's
    _consignment_serial/_submit_invoice seed helpers to prove the Sales
    Report and this module's own GL posting reconcile — report
    cost == GL COGS — for one real submitted invoice.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase


class ConsignorShare(FrappeTestCase):
	"""Pure function: consignor_share(net, pct) = net * (1 - pct/100)."""

	def test_consignor_share_with_commission(self):
		from ffl_core.consignment import consignor_share
		self.assertAlmostEqual(consignor_share(1000, 20), 800.0)

	def test_consignor_share_zero_commission(self):
		from ffl_core.consignment import consignor_share
		self.assertAlmostEqual(consignor_share(1000, 0), 1000.0)

	def test_consignor_share_string_inputs_coerced(self):
		# consignor_share is called with row.base_net_amount / a Percent
		# docfield value, either of which frappe can hand back as a string in
		# some code paths — flt() must absorb that like every other money helper
		# in this codebase (see effective_commission_pct's own flt() calls).
		from ffl_core.consignment import consignor_share
		self.assertAlmostEqual(consignor_share("1000", "20"), 800.0)


class ConsignmentSourceField(FrappeTestCase):
	"""setup/consignment.ensure() must also create the Journal Entry custom
	field B1's GL posting stamps with the source invoice name, so Task B2
	(reversal) can find the JE to cancel."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()

	def test_ensure_creates_journal_entry_source_field(self):
		from ffl_core.setup import consignment
		consignment.ensure()
		self.assertTrue(
			frappe.db.exists("Custom Field", "Journal Entry-ffl_consignment_source"))

	def test_journal_entry_source_field_is_read_only_data(self):
		from ffl_core.setup import consignment
		consignment.ensure()
		field = frappe.get_doc("Custom Field", "Journal Entry-ffl_consignment_source")
		self.assertEqual(field.fieldtype, "Data")
		self.assertEqual(int(field.read_only), 1)


class PostConsignmentPayableDirect(FrappeTestCase):
	"""compliance.consignment.post_consignment_payable called directly over
	lightweight docs — cheap coverage of every skip branch, no real invoice."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Item", "CGL-DIRECT-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "CGL-DIRECT-GUN", "item_name": "CGL-DIRECT-GUN",
				"item_group": "Pistol", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "_Test Consignor Direct"):
			frappe.get_doc({
				"doctype": "Supplier", "supplier_name": "_Test Consignor Direct",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
					or "All Supplier Groups",
				"supplier_type": "Individual",
			}).insert(ignore_permissions=True)
		cls.consignor = "_Test Consignor Direct"

	@staticmethod
	def _ensure_serial(serial: str, *, is_consignment: int = 0, pct: float | None = None,
			consignor: str | None = None, item_code: str = "CGL-DIRECT-GUN") -> str:
		if not frappe.db.exists("Serial No", serial):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": serial, "item_code": item_code,
			}).insert(ignore_permissions=True)
		updates = {"is_consignment": is_consignment}
		if pct is not None:
			updates["consignment_commission_pct"] = pct
		if consignor is not None:
			updates["consignor"] = consignor
		frappe.db.set_value("Serial No", serial, updates, update_modified=False)
		return serial

	@staticmethod
	def _row(**over) -> "frappe._dict":
		base = {"idx": 1, "item_code": "CGL-DIRECT-GUN", "serial_no": "",
			"base_net_amount": 1000.0, "consignment_commission_pct": None,
			"expense_account": None, "cost_center": None}
		base.update(over)
		return frappe._dict(base)

	def _doc(self, *rows, **over):
		# name defaults to the test's own method name so each test's JE lookup
		# (by ffl_consignment_source) can never collide with another test's
		# leftover JE — je.submit() below is not undone between test methods
		# on this runner, so a shared hardcoded name would leak state.
		base = dict(doctype="Sales Invoice", name=self._testMethodName, company=self.company,
			posting_date=frappe.utils.nowdate(), is_return=0, items=list(rows))
		base.update(over)
		return SimpleNamespace(**base)

	def _post(self, doc):
		from ffl_core.compliance.consignment import post_consignment_payable
		post_consignment_payable(doc)

	def test_is_return_invoice_skipped(self):
		# Returns immediately without even touching doc.company — a return
		# reversal is Task B2's job, not this hook's.
		doc = SimpleNamespace(is_return=1)
		self._post(doc)  # must not raise
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))

	def test_consolidated_sales_invoice_skipped(self):
		# A consolidated SI rolls up a POS Invoice that already posted its own
		# JE on submit — posting again here would double the consignor's
		# payable AND break Sales Report reconciliation (it excludes
		# is_consolidated=1 SIs from revenue for the exact same reason; mirrors
		# the existing is_consolidated skip in
		# compliance.firearm.require_4473_and_nics_for_firearm_sale).
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=20,
			consignor=self.consignor)
		doc = self._doc(self._row(serial_no=serial), is_consolidated=1)
		self._post(doc)
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))

	def test_non_consignment_serial_posts_nothing(self):
		serial = self._ensure_serial(self._testMethodName, is_consignment=0)
		doc = self._doc(self._row(serial_no=serial))
		self._post(doc)
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))

	def test_consignment_serial_without_consignor_posts_nothing(self):
		# is_consignment=1 but consignor blank — nothing to book against, and
		# nothing to throw over either (defensive: a partially-stamped serial
		# must not crash a sale).
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=20)
		doc = self._doc(self._row(serial_no=serial))
		self._post(doc)
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))

	def test_zero_share_posts_nothing(self):
		# 100% commission → consignor_share == 0 → nothing owed, no JE.
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=100,
			consignor=self.consignor)
		doc = self._doc(self._row(serial_no=serial))
		self._post(doc)
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))

	def test_row_without_serial_skipped(self):
		doc = self._doc(self._row(item_code="CGL-DIRECT-ACC", serial_no=""))
		self._post(doc)  # must not raise
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))

	def test_consignment_line_posts_balanced_submitted_je(self):
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=20,
			consignor=self.consignor)
		doc = self._doc(self._row(serial_no=serial, base_net_amount=1000.0))
		self._post(doc)

		from ffl_core.setup.consignment import payable_account
		payable = payable_account(self.company)
		je_name = frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1})
		self.assertTrue(je_name)
		je = frappe.get_doc("Journal Entry", je_name)

		cr = [a for a in je.accounts if a.account == payable]
		self.assertEqual(len(cr), 1)
		self.assertAlmostEqual(cr[0].credit_in_account_currency, 800.0)
		self.assertEqual(cr[0].party_type, "Supplier")
		self.assertEqual(cr[0].party, self.consignor)

		dr = [a for a in je.accounts if a.debit_in_account_currency]
		self.assertAlmostEqual(sum(a.debit_in_account_currency for a in dr), 800.0)

	@staticmethod
	def _ensure_consignor(name: str) -> str:
		if not frappe.db.exists("Supplier", name):
			frappe.get_doc({
				"doctype": "Supplier", "supplier_name": name,
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
					or "All Supplier Groups",
				"supplier_type": "Individual",
			}).insert(ignore_permissions=True)
		return name

	def test_multi_serial_line_posts_a_payable_for_every_consignor(self):
		# Two consigned guns of the same model on ONE qty=2 line, each owned by a
		# DIFFERENT consignor. Each gun owes its own consignor its own share of
		# the line: 1000 over 2 units = 500 each, 20% commission -> 400 each.
		# Reading only the first serial paid consignor A the share of BOTH guns
		# (800) and paid consignor B nothing at all.
		other = self._ensure_consignor("_Test Consignor Direct B")
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=20,
			consignor=self.consignor)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=20,
			consignor=other)
		doc = self._doc(self._row(serial_no=f"{a}\n{b}", qty=2, base_net_amount=1000.0))
		self._post(doc)

		from ffl_core.setup.consignment import payable_account
		payable = payable_account(self.company)
		je = frappe.get_doc("Journal Entry", frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1}))
		cr = [x for x in je.accounts if x.account == payable]
		self.assertEqual(len(cr), 2)
		self.assertEqual(
			sorted((x.party, float(x.credit_in_account_currency)) for x in cr),
			sorted([(self.consignor, 400.0), (other, 400.0)]))
		dr = [x for x in je.accounts if x.debit_in_account_currency]
		self.assertAlmostEqual(sum(x.debit_in_account_currency for x in dr), 800.0)
		self.assertAlmostEqual(je.total_debit, je.total_credit)

	def test_multi_serial_line_pays_only_for_the_consigned_gun(self):
		# One consigned gun + one store-owned gun on the same qty=2 line. The
		# consignor is owed for THEIR gun only — one unit's share (500 * 0.8 =
		# 400), not the whole line's (800). This is the half of the bug that
		# OVERPAID: the first-serial read attributed the entire line amount to
		# whichever consignor happened to be first.
		cons = self._ensure_serial(self._testMethodName + "-C", is_consignment=1, pct=20,
			consignor=self.consignor)
		owned = self._ensure_serial(self._testMethodName + "-O", is_consignment=0)
		doc = self._doc(self._row(serial_no=f"{cons}\n{owned}", qty=2, base_net_amount=1000.0))
		self._post(doc)

		from ffl_core.setup.consignment import payable_account
		payable = payable_account(self.company)
		je = frappe.get_doc("Journal Entry", frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1}))
		cr = [x for x in je.accounts if x.account == payable]
		self.assertEqual(len(cr), 1)
		self.assertAlmostEqual(cr[0].credit_in_account_currency, 400.0)
		self.assertEqual(cr[0].party, self.consignor)

	def test_consigned_gun_in_second_position_is_paid(self):
		# The mirror: the consigned gun sits SECOND behind a store-owned gun, so
		# the first-serial read saw a non-consignment serial, skipped the whole
		# line and booked nothing — the consignor was never paid at all.
		owned = self._ensure_serial(self._testMethodName + "-O", is_consignment=0)
		cons = self._ensure_serial(self._testMethodName + "-C", is_consignment=1, pct=20,
			consignor=self.consignor)
		doc = self._doc(self._row(serial_no=f"{owned}\n{cons}", qty=2, base_net_amount=1000.0))
		self._post(doc)

		from ffl_core.setup.consignment import payable_account
		payable = payable_account(self.company)
		je_name = frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1})
		self.assertTrue(je_name, "the consigned gun in position 2 was never booked")
		je = frappe.get_doc("Journal Entry", je_name)
		cr = [x for x in je.accounts if x.account == payable]
		self.assertEqual(len(cr), 1)
		self.assertAlmostEqual(cr[0].credit_in_account_currency, 400.0)

	def test_each_je_row_is_stamped_with_the_gun_it_pays_for(self):
		# One line can now produce several Dr/Cr pairs whose amounts are
		# FRACTIONS of the line, so "which gun is this 400 for?" must be readable
		# off the row itself rather than reverse-engineered from the amount. The
		# serial rides the standard Journal Entry Account.user_remark field.
		other = self._ensure_consignor("_Test Consignor Direct B")
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=20,
			consignor=self.consignor)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=20,
			consignor=other)
		doc = self._doc(self._row(serial_no=f"{a}\n{b}", qty=2, base_net_amount=1000.0))
		self._post(doc)

		je = frappe.get_doc("Journal Entry", frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1}))
		self.assertEqual(
			sorted((x.user_remark or "") for x in je.accounts), sorted([a, a, b, b]))

	def test_a_serial_repeated_on_a_line_is_paid_once(self):
		# A gun can only be consigned-and-sold once per line; a duplicated serial
		# in the row text must not double the consignor's payable.
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=20,
			consignor=self.consignor)
		doc = self._doc(self._row(serial_no=f"{serial}\n{serial}", qty=1,
			base_net_amount=1000.0))
		self._post(doc)

		from ffl_core.setup.consignment import payable_account
		payable = payable_account(self.company)
		je = frappe.get_doc("Journal Entry", frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1}))
		cr = [x for x in je.accounts if x.account == payable]
		self.assertEqual(len(cr), 1)
		self.assertAlmostEqual(cr[0].credit_in_account_currency, 800.0)

	def test_unresolvable_cogs_account_raises_actionable_error(self):
		# Row carries no expense_account/cost_center override, and (temporarily,
		# restored in finally) neither does the company — both resolve blank.
		# Must fail loud with OUR actionable message naming the invoice + item,
		# not ERPNext's raw mandatory-field crash from je.insert()/submit()
		# deep inside the invoice's on_submit.
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=20,
			consignor=self.consignor)
		doc = self._doc(self._row(serial_no=serial))
		original = frappe.db.get_value(
			"Company", self.company, ["default_expense_account", "cost_center"], as_dict=True)
		frappe.db.set_value(
			"Company", self.company,
			{"default_expense_account": None, "cost_center": None}, update_modified=False)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._post(doc)
			message = str(ctx.exception)
			self.assertIn(doc.name, message)
			self.assertIn("CGL-DIRECT-GUN", message)
		finally:
			frappe.db.set_value("Company", self.company, dict(original), update_modified=False)
		self.assertFalse(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName}))


class PostConsignmentPayableInvoice(FrappeTestCase):
	"""End-to-end: a real submitted Sales Invoice selling a consignment gun
	posts the JE through the actual on_submit hook (hooks.py), not just the
	bare function — proves the wiring, matching
	test_consignment_capture.py's FreezeConsignmentCommissionInvoice split
	between direct-call and real-document coverage."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.warehouse = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "is_group": 0}, "name")

		# The task brief flags these as possibly-missing on a from-scratch test
		# company; FFL Test's dev_site.run() Company insert already gets ERPNext's
		# Standard Chart of Accounts (confirmed: default_expense_account =
		# "Cost of Goods Sold - FT", cost_center = "Main - FT"), but seed
		# defensively rather than assume, per the brief.
		if not frappe.db.get_value("Company", cls.company, "default_expense_account"):
			cogs = frappe.db.get_value(
				"Account", {"company": cls.company, "account_type": "Cost of Goods Sold",
					"is_group": 0}, "name")
			frappe.db.set_value("Company", cls.company, "default_expense_account", cogs,
				update_modified=False)
		if not frappe.db.get_value("Company", cls.company, "cost_center"):
			cc = frappe.db.get_value(
				"Cost Center", {"company": cls.company, "is_group": 0}, "name")
			frappe.db.set_value("Company", cls.company, "cost_center", cc, update_modified=False)

		# An ANTIQUE firearm Item: is_regulated_firearm_item excludes it, so a
		# submitted Sales Invoice selling it skips require_4473_and_nics_for_firearm_sale
		# and require_shippable_ffl_dealer entirely (both gate on regulated-only
		# rows) — no FastBound 4473, no ATF FFL Record, no dealer customer needed.
		# It's still is_firearm_item (serialized, require_serial_for_firearm
		# still applies) and still consignable (is_consignment lives on the
		# Serial No, independent of antique status).
		if not frappe.db.exists("Item", "CGL-ANTIQUE-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "CGL-ANTIQUE-GUN", "item_name": "CGL-ANTIQUE-GUN",
				"item_group": "Pistol", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
			frappe.db.set_value("Item", "CGL-ANTIQUE-GUN", "is_antique", 1, update_modified=False)
		if not frappe.db.exists("Customer", "_Test Consignment GL Buyer"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Consignment GL Buyer",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "_Test Consignor GL"):
			frappe.get_doc({
				"doctype": "Supplier", "supplier_name": "_Test Consignor GL",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
					or "All Supplier Groups",
				"supplier_type": "Individual",
			}).insert(ignore_permissions=True)
		cls.consignor = "_Test Consignor GL"

	def _consignment_serial(self, serial: str, pct: float = 20.0) -> str:
		from ffl_core.firearm import serialized_receipt_row
		if not frappe.db.exists("Serial No", serial):
			se = frappe.get_doc({
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [serialized_receipt_row(
					item_code="CGL-ANTIQUE-GUN", qty=1, warehouse=self.warehouse,
					basic_rate=500, serial=serial)],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
		frappe.db.set_value("Serial No", serial, {
			"is_consignment": 1, "consignment_commission_pct": pct, "consignor": self.consignor,
		}, update_modified=False)
		return serial

	def _submit_invoice(self, serial: str, net: float = 1000.0) -> "frappe.Document":
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = "_Test Consignment GL Buyer"
		si.append("items", {
			"item_code": "CGL-ANTIQUE-GUN", "qty": 1, "rate": net,
			"warehouse": self.warehouse, "serial_no": serial, "use_serial_batch_fields": 1,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si

	def test_submit_posts_consignor_payable_je(self):
		serial = self._consignment_serial(self._testMethodName)
		si = self._submit_invoice(serial, net=1000.0)
		self.assertEqual(si.docstatus, 1)
		self.assertAlmostEqual(float(si.items[0].base_net_amount), 1000.0)

		je_name = frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": si.name, "docstatus": 1})
		self.assertTrue(je_name)
		je = frappe.get_doc("Journal Entry", je_name)

		from ffl_core.setup.consignment import payable_account
		payable = payable_account(si.company)
		cr = [a for a in je.accounts if a.account == payable]
		self.assertEqual(len(cr), 1)
		self.assertAlmostEqual(cr[0].credit_in_account_currency, 800.0)  # 1000*(1-0.20)
		self.assertEqual(cr[0].party_type, "Supplier")
		self.assertEqual(cr[0].party, self.consignor)

		dr = [a for a in je.accounts if a.debit_in_account_currency]
		self.assertAlmostEqual(sum(a.debit_in_account_currency for a in dr), 800.0)  # balanced
		self.assertAlmostEqual(je.total_debit, je.total_credit)

	def test_non_consignment_sale_posts_no_je(self):
		"""Wiring sanity check: a plain (non-consignment) firearm sale through
		the SAME on_submit hook must not create a Journal Entry at all."""
		serial = self._testMethodName
		from ffl_core.firearm import serialized_receipt_row
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code="CGL-ANTIQUE-GUN", qty=1, warehouse=self.warehouse,
				basic_rate=500, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		si = self._submit_invoice(serial, net=750.0)
		self.assertFalse(
			frappe.db.exists("Journal Entry", {"ffl_consignment_source": si.name}))

	def test_cancel_reverses_consignor_payable_je(self):
		"""B2 — cancelling the consignment sale must cancel the payable JE B1
		posted at submit, so the payable + COGS unwind. _submit_invoice never
		pays the invoice and never sets update_stock, so there's no linked
		Payment Entry / Delivery Note to block a clean si.cancel() here — the
		JE itself is found only via ffl_consignment_source (a plain Data
		field, not a standard GL/party reference), so ERPNext's own
		cancel-time linked-doc check can't see it; reversing it is entirely
		this hook's job."""
		serial = self._consignment_serial(self._testMethodName)
		si = self._submit_invoice(serial, net=1000.0)
		self.assertTrue(frappe.db.exists(
			"Journal Entry", {"ffl_consignment_source": si.name, "docstatus": 1}))

		si.cancel()

		je_status = frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": si.name}, "docstatus")
		self.assertEqual(je_status, 2)  # cancelled

	def test_sales_report_cost_reconciles_with_gl_cogs(self):
		"""Reconciliation: the Sales Report's consignment cost for
		this invoice must equal the sum of the debit side of the posted Journal
		Entry (its COGS postings). Both sides compute the exact same
		consignor_share(net, pct) = P*(1-c%) — the report via
		report/sales_report.py's _line_cost, the GL via
		compliance.consignment.post_consignment_payable — so they must agree
		by construction; this proves it end-to-end against a REAL submitted,
		non-consolidated Sales Invoice. Non-consolidated matters: a
		consolidated Sales Invoice (is_consolidated=1) is excluded from the
		report's header query (sales_report.py's _fetch_headers only takes
		Sales Invoice rows with is_consolidated=0, channel "Order") — and
		_submit_invoice's plain Sales Invoice is never consolidated, so it
		qualifies."""
		serial = self._consignment_serial(self._testMethodName)
		si = self._submit_invoice(serial, net=1000.0)

		from ffl_core.ffl_core.report.sales_report.sales_report import execute
		# execute() returns Frappe's native 5-tuple (r3); only columns + data needed here.
		cols, rows, *_ = execute({"from_date": si.posting_date, "to_date": si.posting_date})
		row = next(r for r in rows if r.get("invoice") == si.name)

		je_name = frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": si.name, "docstatus": 1})
		je = frappe.get_doc("Journal Entry", je_name)
		gl_cogs = sum(a.debit_in_account_currency for a in je.accounts)

		self.assertAlmostEqual(row["cost"], gl_cogs)   # report cost == GL COGS
		self.assertAlmostEqual(row["cost"], 800.0)     # both = 1000*(1-0.20)
		self.assertAlmostEqual(row["profit"], 200.0)   # commission


class ReverseConsignmentPayableDirect(FrappeTestCase):
	"""compliance.consignment.reverse_consignment_payable called directly —
	cheap coverage of the Global Constraints (idempotent/safe: no-op when no
	matching JE exists, and safe to call again once already reversed) without
	a real invoice cancel. Seeds its submitted JE via post_consignment_payable
	itself — the same lightweight-doc pattern PostConsignmentPayableDirect
	uses above — since reverse_consignment_payable only cares about a Journal
	Entry's ffl_consignment_source + docstatus, not how it was produced."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Item", "CGL-REVERSE-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "CGL-REVERSE-GUN", "item_name": "CGL-REVERSE-GUN",
				"item_group": "Pistol", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "_Test Consignor Reverse"):
			frappe.get_doc({
				"doctype": "Supplier", "supplier_name": "_Test Consignor Reverse",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
					or "All Supplier Groups",
				"supplier_type": "Individual",
			}).insert(ignore_permissions=True)
		cls.consignor = "_Test Consignor Reverse"

	def _seed_submitted_je(self, source: str) -> None:
		from ffl_core.compliance.consignment import post_consignment_payable
		if not frappe.db.exists("Serial No", source):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": source, "item_code": "CGL-REVERSE-GUN",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", source, {
			"is_consignment": 1, "consignment_commission_pct": 20, "consignor": self.consignor,
		}, update_modified=False)
		row = frappe._dict({"idx": 1, "item_code": "CGL-REVERSE-GUN", "serial_no": source,
			"base_net_amount": 1000.0, "consignment_commission_pct": None,
			"expense_account": None, "cost_center": None})
		doc = SimpleNamespace(doctype="Sales Invoice", name=source, company=self.company,
			posting_date=frappe.utils.nowdate(), is_return=0, items=[row])
		post_consignment_payable(doc)

	def test_no_matching_je_is_a_no_op(self):
		# Non-consignment invoice (or any doc with no consignment JE at all)
		# — must not raise.
		from ffl_core.compliance.consignment import reverse_consignment_payable
		reverse_consignment_payable(SimpleNamespace(name=self._testMethodName))

	def test_cancels_matching_submitted_je(self):
		from ffl_core.compliance.consignment import reverse_consignment_payable
		self._seed_submitted_je(self._testMethodName)
		je_name = frappe.db.get_value(
			"Journal Entry", {"ffl_consignment_source": self._testMethodName, "docstatus": 1})
		self.assertTrue(je_name)

		reverse_consignment_payable(SimpleNamespace(name=self._testMethodName))

		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)

	def test_second_call_is_a_no_op_once_already_cancelled(self):
		# Already reversed (e.g. a re-fired hook, or cancel-then-cancel via the
		# desk) — nothing left at docstatus 1 to find; must not raise.
		from ffl_core.compliance.consignment import reverse_consignment_payable
		self._seed_submitted_je(self._testMethodName)
		doc = SimpleNamespace(name=self._testMethodName)
		reverse_consignment_payable(doc)
		reverse_consignment_payable(doc)


class BlockConsignmentReturnsDirect(FrappeTestCase):
	"""compliance.consignment.block_consignment_returns called directly over
	lightweight docs (same SimpleNamespace-doc / frappe._dict-rows pattern as
	PostConsignmentPayableDirect above — _line_serials' serials_for_line(row)
	needs row.get()).

	Owner policy: a consignment gun is never returnable — undoing a
	consignment sale is exclusively done by CANCELLING the original invoice
	(which reverse_consignment_payable/B2 already unwinds), never by ringing a
	return. This is a hard block, not a submit-only gate like the trade-in/
	deposit rows: there's no legitimate transient draft state for a
	consignment return the way a mid-edit cart can transiently mismatch, so
	the check fires on every validate (draft save included), not just submit."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Item", "CGL-BLOCKRET-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "CGL-BLOCKRET-GUN", "item_name": "CGL-BLOCKRET-GUN",
				"item_group": "Pistol", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_serial(serial: str, *, is_consignment: int = 0,
			item_code: str = "CGL-BLOCKRET-GUN") -> str:
		if not frappe.db.exists("Serial No", serial):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": serial, "item_code": item_code,
			}).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Serial No", serial, "is_consignment", is_consignment, update_modified=False)
		return serial

	@staticmethod
	def _row(**over) -> "frappe._dict":
		base = {"idx": 1, "item_code": "CGL-BLOCKRET-GUN", "serial_no": ""}
		base.update(over)
		return frappe._dict(base)

	def _doc(self, *rows, **over) -> SimpleNamespace:
		base = dict(doctype="Sales Invoice", name=self._testMethodName, is_return=0,
			items=list(rows))
		base.update(over)
		return SimpleNamespace(**base)

	def _block(self, doc) -> None:
		from ffl_core.compliance.consignment import block_consignment_returns
		block_consignment_returns(doc)

	def test_non_return_invoice_with_consignment_line_untouched(self):
		# A normal (non-return) consignment sale must sail through this gate
		# — it's not the one blocking anything here (post_consignment_payable
		# handles the sale itself).
		serial = self._ensure_serial(self._testMethodName, is_consignment=1)
		doc = self._doc(self._row(serial_no=serial), is_return=0)
		self._block(doc)  # must not raise

	def test_return_invoice_without_consignment_line_untouched(self):
		# An ordinary (non-consignment) return is unaffected by owner policy.
		serial = self._ensure_serial(self._testMethodName, is_consignment=0)
		doc = self._doc(self._row(serial_no=serial), is_return=1)
		self._block(doc)  # must not raise

	def test_return_invoice_with_consignment_line_raises(self):
		serial = self._ensure_serial(self._testMethodName, is_consignment=1)
		doc = self._doc(self._row(serial_no=serial), is_return=1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._block(doc)
		message = str(ctx.exception)
		self.assertIn("cannot be returned", message)
		self.assertIn(self._testMethodName, message)

	def test_return_invoice_row_without_serial_skipped(self):
		# Defensive: a return row with no serial (e.g. a non-firearm
		# accessory) has nothing to look up — must not raise.
		doc = self._doc(self._row(item_code="CGL-BLOCKRET-ACC", serial_no=""), is_return=1)
		self._block(doc)  # must not raise

	def test_return_with_a_consignment_gun_in_second_position_raises(self):
		# A qty>1 line carries every gun's serial in ONE row (web-order fulfilment
		# and counter multi-gun orders both build that shape). Reading only the
		# row's FIRST serial let a consignment gun ride along in position 2+ and
		# slip past the never-returnable policy entirely — the return would be
		# accepted, and its consignor payable left standing.
		plain = self._ensure_serial(self._testMethodName + "-P", is_consignment=0)
		cons = self._ensure_serial(self._testMethodName + "-C", is_consignment=1)
		doc = self._doc(self._row(serial_no=f"{plain}\n{cons}", qty=2), is_return=1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._block(doc)
		self.assertIn("cannot be returned", str(ctx.exception))

	def test_return_invoice_with_mixed_rows_still_raises(self):
		# One ordinary row + one consignment row on the same return — the
		# consignment row alone is enough to block the whole invoice.
		plain_serial = self._ensure_serial(self._testMethodName + "-P", is_consignment=0)
		cons_serial = self._ensure_serial(self._testMethodName + "-C", is_consignment=1)
		doc = self._doc(
			self._row(idx=1, serial_no=plain_serial),
			self._row(idx=2, serial_no=cons_serial),
			is_return=1,
		)
		with self.assertRaises(frappe.ValidationError):
			self._block(doc)


if __name__ == "__main__":
	unittest.main()
