"""Native shipping charge: a Sales Taxes and Charges "Actual" row driven by the
ffl_shipping_amount custom field, surviving the interstate-tax template swap.

Mirrors tests/test_interstate_tax.py's harness (default 8.25% template + the 0%
Out of State template), so tax numbers are deterministic.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

from ffl_core.tests.settings_sandbox import SettingsSandbox

CUSTOMER = "_Test Shipping Buyer"

# Deliberate test-side literal (not imported from the module): pins the wire
# format of the freight tax row's identifying description PREFIX (the full
# description carries a rate suffix, e.g. "Shipping Tax (8.25%)").
SHIPPING_TAX_DESCRIPTION = "Shipping Tax"


class ShippingChargeBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls._ensure_tax_account_and_default_template()
		from ffl_core.setup import tax as tax_setup
		from ffl_core.setup import shipping_charge
		tax_setup.ensure()
		shipping_charge.ensure()
		cls.default_template = f"Texas Sales Tax - {cls.abbr}"
		cls.oos_template = f"Out of State Sales - {cls.abbr}"
		cls.ship_account = f"Shipping Income - {cls.abbr}"
		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "SHIP-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "SHIP-ACC", "item_name": "SHIP-ACC",
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_tax_account_and_default_template(cls) -> None:
		acct_name = frappe.db.get_value(
			"Account", {"company": cls.company, "account_name": "SHIP Sales Tax"}, "name")
		if not acct_name:
			parent = frappe.db.get_value(
				"Account", {"company": cls.company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			acct = frappe.get_doc({
				"doctype": "Account", "account_name": "SHIP Sales Tax",
				"company": cls.company, "parent_account": parent, "account_type": "Tax",
			})
			acct.insert(ignore_permissions=True)
			acct_name = acct.name
		cls.tax_account = acct_name
		if not frappe.db.exists("Sales Taxes and Charges Template",
				f"Texas Sales Tax - {cls.abbr}"):
			frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": "Texas Sales Tax", "company": cls.company, "is_default": 1,
				"taxes": [{"charge_type": "On Net Total", "account_head": acct_name,
					"rate": 8.25, "description": "TX Sales Tax"}],
			}).insert(ignore_permissions=True)


class SetupShippingCharge(ShippingChargeBase):
	def test_custom_field_exists_on_so_and_si(self):
		for dt in ("Sales Order", "Sales Invoice"):
			self.assertTrue(
				frappe.db.exists("Custom Field", {"dt": dt, "fieldname": "ffl_shipping_amount"}),
				f"{dt} missing ffl_shipping_amount")

	def test_shipping_income_account_created_under_income(self):
		self.assertTrue(frappe.db.exists("Account", self.ship_account))
		self.assertEqual(
			frappe.db.get_value("Account", self.ship_account, "root_type"), "Income")
		self.assertEqual(frappe.db.get_value("Account", self.ship_account, "is_group"), 0)

	def test_ensure_is_idempotent(self):
		from ffl_core.setup import shipping_charge
		shipping_charge.ensure()  # second run must not error or duplicate
		accts = frappe.get_all("Account",
			filters={"company": self.company, "account_name": "Shipping Income"})
		self.assertEqual(len(accts), 1)


class ShippingChargeOnDocs(ShippingChargeBase):
	def _address(self, title: str, state: str, country: str = "United States") -> str:
		name = frappe.db.get_value("Address", {"address_title": title}, "name")
		if name:
			frappe.db.set_value("Address", name, {"state": state, "country": country},
				update_modified=False)
			return name
		return frappe.get_doc({
			"doctype": "Address", "address_title": title, "address_type": "Shipping",
			"address_line1": "1 Test St", "city": "Testville",
			"state": state, "country": country,
			"links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
		}).insert(ignore_permissions=True).name

	def _so(self, shipping_address, shipping_amount):
		"""Build the SO the way production reaches the hook: the default template
		is pre-pinned (set_missing_values short-circuits under frappe.in_test)."""
		from erpnext.controllers.accounts_controller import get_taxes_and_charges
		so = frappe.new_doc("Sales Order")
		so.company = self.company
		so.customer = CUSTOMER
		so.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
		so.taxes_and_charges = self.default_template
		so.set("taxes", get_taxes_and_charges(
			"Sales Taxes and Charges Template", self.default_template))
		so.append("items", {"item_code": "SHIP-ACC", "qty": 1, "rate": 100})
		if shipping_address:
			so.shipping_address_name = shipping_address
		so.ffl_shipping_amount = shipping_amount
		# These pin the add-on-top basis; the inclusive matrix is ShippingTaxMirror i1–i11.
		so.flags.ffl_shipping_tax_basis_set = True
		so.flags.ignore_permissions = True
		so.insert()
		return so

	def _freight_rows(self, doc):
		return [t for t in doc.taxes if t.account_head == self.ship_account]

	def test_out_of_state_shipping_survives_template_swap(self):
		"""The regression guard: an out-of-state address makes the tax engine
		REPLACE the taxes table with the 0% OOS template; the freight row must be
		re-added afterward and the order taxed at 0 + shipping."""
		so = self._so(self._address("SHIP-CA", "California"), 50)
		self.assertEqual(so.taxes_and_charges, self.oos_template)
		rows = self._freight_rows(so)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].charge_type, "Actual")
		self.assertEqual(flt(rows[0].tax_amount), 50.0)
		self.assertEqual(flt(so.net_total), 100.0)            # shipping not in the taxed base
		self.assertEqual(flt(so.total_taxes_and_charges), 50.0)  # 0 tax + 50 shipping
		self.assertEqual(flt(so.grand_total), 150.0)

	def test_in_state_shipping_added_on_top_of_tax(self):
		so = self._so(self._address("SHIP-TX", "Texas"), 50)
		self.assertEqual(so.taxes_and_charges, self.default_template)
		self.assertEqual(len(self._freight_rows(so)), 1)
		self.assertEqual(flt(so.net_total), 100.0)            # 8.25% applies to 100, not 150
		# In-state freight is taxed too — 8.25 sales tax + 50 shipping
		# + 8.25% × 50 freight tax (flt() so the site's rounding method decides
		# the 4.125 midpoint).
		freight_tax = flt(50 * 8.25 / 100.0, 2)
		self.assertEqual(flt(so.total_taxes_and_charges), flt(58.25 + freight_tax, 2))
		self.assertEqual(flt(so.grand_total), flt(158.25 + freight_tax, 2))

	def test_zero_shipping_adds_no_row(self):
		so = self._so(self._address("SHIP-TX0", "Texas"), 0)
		self.assertEqual(len(self._freight_rows(so)), 0)
		self.assertEqual(flt(so.total_taxes_and_charges), 8.25)  # tax only
		self.assertEqual(flt(so.grand_total), 108.25)

	def test_changed_amount_replaces_row_not_duplicates(self):
		so = self._so(self._address("SHIP-TXUP", "Texas"), 50)
		self.assertEqual(len(self._freight_rows(so)), 1)
		so.ffl_shipping_amount = 75
		so.save()
		rows = self._freight_rows(so)
		self.assertEqual(len(rows), 1)
		self.assertEqual(flt(rows[0].tax_amount), 75.0)
		# + 8.25% × 75 = 6.19 freight tax
		self.assertEqual(flt(so.grand_total), flt(183.25 + 6.19, 2))  # 100 + 8.25 tax + 75 shipping + 6.19


class CreateOrderShipping(SettingsSandbox, ShippingChargeBase):
	def _buyer(self, name: str) -> str:
		"""A dedicated individual per test — create_order's submit chain commits,
		so a shared customer's set_customer_tax_exempt would leak across tests
		(see tests/test_build_order.py::_indiv)."""
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		return name

	def test_create_order_carries_shipping_to_invoice(self):
		from ffl_core.api.manual_order import create_order
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.assertTrue(pos, "dev_site should seed a POS Profile for the test company")
		out = create_order({
			"buyer_type": "individual", "customer": self._buyer("_Test Ship CO Buyer"),
			"pos_profile": pos, "dispose_now": 0, "payment_received": 0, "tax_exempt": 1,
			"shipping_amount": 35,
			"items": [{"item_code": "SHIP-ACC", "qty": 1, "rate": 100}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(flt(si.ffl_shipping_amount), 35.0)
		rows = [t for t in si.taxes if t.account_head == self.ship_account]
		self.assertEqual(len(rows), 1)
		self.assertEqual(flt(rows[0].tax_amount), 35.0)
		self.assertEqual(flt(si.grand_total), 135.0)  # 100 goods + 35 shipping, tax exempt
		self.assertEqual(
			flt(frappe.db.get_value("Sales Order", out["sales_order"], "ffl_shipping_amount")), 35.0)

	def test_create_order_stamps_the_shipping_tax_switch(self):
		"""A new taxed order takes FFL Settings'
		switch (ON when never saved) onto both SO and SI — 899 + S=45 totals
		1018.17 inclusive, 1021.88 on the old add-on-top basis — and
		quote_config tells the Create Order preview the same."""
		from ffl_core.api.manual_order import create_order, quote_config
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		for switch, flag, grand in ((None, 1, 1018.17), (0, 0, 1021.88)):
			with self.subTest(switch=switch):
				self.set_single("FFL Settings", "shipping_price_includes_tax", switch)
				self.assertIs(quote_config(self.company)["shipping_tax_inclusive"], bool(flag))
				out = create_order({
					"buyer_type": "individual", "customer": self._buyer("_Test Ship Incl Buyer"),
					"pos_profile": pos, "dispose_now": 0, "payment_received": 0,
					"tax_exempt": 0, "shipping_amount": 45,
					"items": [{"item_code": "SHIP-ACC", "qty": 1, "rate": 899}],
				})
				si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
				self.assertEqual(si.ffl_shipping_tax_inclusive, flag)
				self.assertEqual(frappe.db.get_value(
					"Sales Order", out["sales_order"], "ffl_shipping_tax_inclusive"), flag)
				self.assertEqual(flt(si.grand_total), grand)

	def test_create_order_refuses_a_stale_shipping_tax_basis(self):
		"""r2 #2 — the page sends the basis it priced on; if FFL Settings changed
		since, a new order with shipping is refused (and nothing is created)
		instead of booking a total the customer was never shown."""
		from ffl_core.api.manual_order import create_order
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.set_single("FFL Settings", "shipping_price_includes_tax", 1)

		def order(basis, shipping):
			return create_order({
				"buyer_type": "individual", "customer": self._buyer("_Test Ship Stale Buyer"),
				"pos_profile": pos, "dispose_now": 0, "payment_received": 0,
				"tax_exempt": 0, "shipping_amount": shipping, "shipping_tax_inclusive": basis,
				"items": [{"item_code": "SHIP-ACC", "qty": 1, "rate": 899}],
			})

		before = frappe.db.count("Sales Order")
		with self.assertRaisesRegex(frappe.ValidationError, "reload the page"):
			order(0, 45)
		self.assertEqual(frappe.db.count("Sales Order"), before)
		self.assertTrue(order(1, 45)["ok"])  # the page agrees with the setting
		self.assertTrue(order(0, 0)["ok"])   # no shipping: nothing to mis-price

	def test_install_stores_the_switch_default_once(self):
		"""r2 #3 — a site with no stored row (fresh install: patches don't run)
		gets ON written, so the form and single_flag agree; a stated OFF stays."""
		from ffl_core.setup.shipping_charge import ensure_shipping_tax_switch
		from ffl_core.tests.settings_sandbox import read_raw_single
		self.clear_single("FFL Settings", "shipping_price_includes_tax")
		ensure_shipping_tax_switch()
		self.assertEqual(str(read_raw_single("FFL Settings", "shipping_price_includes_tax")), "1")
		self.assertEqual(frappe.get_doc("FFL Settings").shipping_price_includes_tax, 1)
		self.set_single("FFL Settings", "shipping_price_includes_tax", 0)
		ensure_shipping_tax_switch()
		self.assertEqual(str(read_raw_single("FFL Settings", "shipping_price_includes_tax")), "0")
		self.assertIn("ffl_core.setup.shipping_charge.ensure_shipping_tax_switch",
			frappe.get_hooks("after_install", app_name="ffl_core"))

	def test_negative_shipping_is_rejected(self):
		from ffl_core.api.manual_order import create_order
		with self.assertRaises(frappe.ValidationError):
			create_order({
				"buyer_type": "individual", "customer": self._buyer("_Test Ship Neg Buyer"),
				"dispose_now": 0, "payment_received": 0, "tax_exempt": 1,
				"shipping_amount": -5,
				"items": [{"item_code": "SHIP-ACC", "qty": 1, "rate": 100}],
			})


class SeedShippingChargePatch(ShippingChargeBase):
	def _seed_shipping_item(self, is_stock: int) -> None:
		"""Put the stray "Shipping" Item in a known state (created if absent)."""
		if frappe.db.exists("Item", "Shipping"):
			frappe.db.set_value("Item", "Shipping",
				{"is_stock_item": is_stock, "is_used": 0, "disabled": 0},
				update_modified=False)
		else:
			frappe.get_doc({
				"doctype": "Item", "item_code": "Shipping", "item_name": "Shipping",
				"item_group": "Service", "stock_uom": "Nos",
				"is_stock_item": is_stock, "has_serial_no": 0,
			}).insert(ignore_permissions=True)

	def test_patch_disables_stray_stock_shipping_item(self):
		from ffl_core.patches import seed_shipping_charge
		self._seed_shipping_item(is_stock=1)
		seed_shipping_charge.execute()
		self.assertEqual(frappe.db.get_value("Item", "Shipping", "disabled"), 1)

	def test_patch_is_idempotent(self):
		from ffl_core.patches import seed_shipping_charge
		self._seed_shipping_item(is_stock=1)
		seed_shipping_charge.execute()
		seed_shipping_charge.execute()  # re-run must not error or duplicate
		self.assertEqual(frappe.db.get_value("Item", "Shipping", "disabled"), 1)
		accts = frappe.get_all("Account",
			filters={"company": self.company, "account_name": "Shipping Income"})
		self.assertEqual(len(accts), 1)

	def test_patch_leaves_non_stock_shipping_item_alone(self):
		from ffl_core.patches import seed_shipping_charge
		self._seed_shipping_item(is_stock=0)
		seed_shipping_charge.execute()
		self.assertEqual(frappe.db.get_value("Item", "Shipping", "disabled"), 0)


class ShippingTaxMirror(SettingsSandbox, ShippingChargeBase):
	"""An in-state taxable
	order must tax its freight at the sales-tax rate — every sales-tax template
	row that computed a positive goods tax gets a freight
	tax row: charge_type "Actual", amount hook-computed = rate% × the FULL
	freight (the OPRA kernel mechanism prorated per item through Item Tax
	Template overrides — 3.54 on mixed orders, 6.19 on trade-in shapes — and
	was abandoned). Out-of-state / exempt-customer / all-exempt-lines orders
	and woo revenue SIs stay untouched. Matrix T1–T12;
	dollar figures are real numbers where given."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.exempt_itt = cls._ensure_exempt_itt()
		cls.svc_item = cls._ensure_service_item()
		cls.tradein_item = cls._ensure_tradein_item()
		cls.city_account, cls.multi_template = cls._ensure_state_city_template()

	# -------------------------------------------------------------- fixtures

	@classmethod
	def _ensure_exempt_itt(cls) -> str:
		"""A 0% Item Tax Template on the sales-tax account — the Non-Taxable
		Service mechanism, self-contained so this class owns its fixture."""
		name = f"_Test Ship Exempt - {cls.abbr}"
		if not frappe.db.exists("Item Tax Template", name):
			frappe.get_doc({
				"doctype": "Item Tax Template", "title": "_Test Ship Exempt",
				"company": cls.company,
				"taxes": [{"tax_type": cls.tax_account, "tax_rate": 0}],
			}).insert(ignore_permissions=True)
		return name

	@classmethod
	def _ensure_service_item(cls) -> str:
		code = "_TEST-SHIPTAX-SVC"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
				# item-level attachment makes the row-level template "allowed"
				# for ERPNext's validate_item_tax_template
				"taxes": [{"item_tax_template": cls.exempt_itt}],
			}).insert(ignore_permissions=True)
		return code

	@classmethod
	def _ensure_tradein_item(cls) -> str:
		"""TRADE-IN-CREDIT's exact tax shape (0% ITT + negative line amount),
		self-contained: a non-stock credit item carrying the 0% template."""
		code = "_TEST-SHIPTAX-TRADEIN"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
				"taxes": [{"item_tax_template": cls.exempt_itt}],
			}).insert(ignore_permissions=True)
		return code

	@classmethod
	def _ensure_state_city_template(cls) -> tuple[str, str]:
		"""T10's forward-looking fixture: a state+city two-row template."""
		city = frappe.db.get_value(
			"Account", {"company": cls.company, "account_name": "SHIP City Tax"}, "name")
		if not city:
			parent = frappe.db.get_value(
				"Account", {"company": cls.company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			city = frappe.get_doc({
				"doctype": "Account", "account_name": "SHIP City Tax",
				"company": cls.company, "parent_account": parent, "account_type": "Tax",
			}).insert(ignore_permissions=True).name
		tmpl = f"_Test TX State City - {cls.abbr}"
		if not frappe.db.exists("Sales Taxes and Charges Template", tmpl):
			frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": "_Test TX State City", "company": cls.company, "is_default": 0,
				"taxes": [
					{"charge_type": "On Net Total", "account_head": cls.tax_account,
						"rate": 6.25, "description": "TX State Tax"},
					{"charge_type": "On Net Total", "account_head": city,
						"rate": 2.00, "description": "City Tax"},
				],
			}).insert(ignore_permissions=True)
		return city, tmpl

	# -------------------------------------------------------------- builders

	def _address(self, title: str, state: str, country: str = "United States") -> str:
		name = frappe.db.get_value("Address", {"address_title": title}, "name")
		if name:
			frappe.db.set_value("Address", name, {"state": state, "country": country},
				update_modified=False)
			return name
		return frappe.get_doc({
			"doctype": "Address", "address_title": title, "address_type": "Shipping",
			"address_line1": "1 Test St", "city": "Testville",
			"state": state, "country": country,
			"links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
		}).insert(ignore_permissions=True).name

	def _so(self, shipping_address, shipping_amount, items=None, template=None,
			customer=None, inclusive=0):
		"""ShippingChargeOnDocs._so's production shape, plus per-line item tax
		templates (T4/T5) and a taxes template override (T10)."""
		from erpnext.controllers.accounts_controller import get_taxes_and_charges
		so = frappe.new_doc("Sales Order")
		so.company = self.company
		so.customer = customer or CUSTOMER
		so.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
		tmpl = template or self.default_template
		so.taxes_and_charges = tmpl
		so.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", tmpl))
		for code, qty, rate, itt in items or [("SHIP-ACC", 1, 100.0, None)]:
			row = so.append("items", {"item_code": code, "qty": qty, "rate": rate})
			if itt:
				row.item_tax_template = itt
		if shipping_address:
			so.shipping_address_name = shipping_address
		so.ffl_shipping_amount = shipping_amount
		so.ffl_shipping_tax_inclusive = inclusive
		so.flags.ffl_shipping_tax_basis_set = True  # pinned, as create_order pins it
		so.flags.ignore_permissions = True
		so.insert()
		return so

	def _si(self, shipping_amount, items=None, woo_online_order=None, woo_tax=None,
			extra_taxes=None, inclusive=0):
		"""A real Sales Invoice through the full validate chain. Passing
		woo_online_order builds the revenue.py web-order shape instead:
		no template, the Woo-collected tax mirrored as an Actual row."""
		from erpnext.controllers.accounts_controller import get_taxes_and_charges
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = CUSTOMER
		si.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		si.conversion_rate = 1
		si.debit_to = self._receivable()
		si.due_date = frappe.utils.nowdate()
		si.update_stock = 0
		if woo_online_order:
			si.woo_online_order = woo_online_order
			si.flags.ignore_links = True  # stub order name; the hook only checks truthiness
		else:
			si.taxes_and_charges = self.default_template
			si.set("taxes", get_taxes_and_charges(
				"Sales Taxes and Charges Template", self.default_template))
		for code, qty, rate, itt in items or [("SHIP-ACC", 1, 100.0, None)]:
			row = si.append("items", {"item_code": code, "qty": qty, "rate": rate})
			if itt:
				row.item_tax_template = itt
		if woo_tax:
			si.append("taxes", {
				"charge_type": "Actual", "account_head": self.tax_account,
				"description": "Sales tax collected by WooCommerce (Woo #931)",
				"tax_amount": woo_tax,
			})
		for t in (extra_taxes or []):
			si.append("taxes", t)
		si.ffl_shipping_amount = shipping_amount
		si.ffl_shipping_tax_inclusive = inclusive
		si.flags.ffl_shipping_tax_basis_set = True  # pinned, as create_order pins it
		si.flags.ignore_permissions = True
		si.insert()
		return si

	def _receivable(self) -> str:
		return (frappe.db.get_value("Company", self.company, "default_receivable_account")
			or frappe.db.get_value("Account",
				{"company": self.company, "account_type": "Receivable", "is_group": 0},
				"name"))

	# --------------------------------------------------------------- helpers

	def _freight_rows(self, doc):
		return [t for t in doc.taxes if t.account_head == self.ship_account]

	def _mirror_rows(self, doc):
		return [t for t in doc.taxes
			if (t.description or "").startswith(SHIPPING_TAX_DESCRIPTION)]

	def _goods_tax_rows(self, doc):
		"""Sales-tax rows that tax the goods — everything that is neither the
		freight row nor a freight tax row."""
		return [t for t in doc.taxes
			if t.account_head != self.ship_account
			and not (t.description or "").startswith(SHIPPING_TAX_DESCRIPTION)]

	def _gl(self, voucher_no: str, account: str) -> float:
		rows = frappe.get_all("GL Entry",
			filters={"voucher_no": voucher_no, "account": account, "is_cancelled": 0},
			fields=["credit", "debit"])
		return flt(sum(flt(g.credit) - flt(g.debit) for g in rows), 2)

	def _assert_single_mirror(self, doc, rate_text: str, amount: float):
		"""The one freight tax row, correct in every identifying feature —
		charge_type "Actual" (the kernel nulls `rate`
		and forbids row_id on Actual rows) + fixed description prefix with a
		rate-text suffix + the sales-tax liability account."""
		freight = self._freight_rows(doc)
		self.assertEqual(len(freight), 1)
		mirrors = self._mirror_rows(doc)
		self.assertEqual(len(mirrors), 1)
		m = mirrors[0]
		self.assertEqual(m.charge_type, "Actual")
		self.assertFalse(m.row_id)
		self.assertTrue((m.description or "").startswith(SHIPPING_TAX_DESCRIPTION))
		self.assertIn(f"({rate_text}%)", m.description)
		self.assertEqual(m.account_head, self.tax_account)
		self.assertEqual(flt(m.tax_amount, 2), amount)
		return m

	# ------------------------------------------------------------- T1 – T10

	def test_t1_in_state_taxable_order_taxes_freight(self):
		"""Real numbers: 664.00 goods + 45.00 freight
		at 8.25%. Freight tax must be 3.71 = 8.25% × 45.00 exactly — never a
		running-total base (63.01 tax-on-tax) and never an item-prorated base;
		the hook-computed Actual row is structurally immune to both."""
		si = self._si(45.0, items=[("SHIP-ACC", 1, 664.0, None)])
		goods = self._goods_tax_rows(si)
		self.assertEqual(len(goods), 1)
		self.assertEqual(flt(goods[0].tax_amount, 2), 54.78)
		self._assert_single_mirror(si, rate_text="8.25", amount=3.71)
		self.assertEqual(flt(si.total_taxes_and_charges), 103.49)  # 54.78 + 45 + 3.71
		self.assertEqual(flt(si.grand_total), 767.49)
		si.submit()
		self.assertEqual(self._gl(si.name, self.tax_account), 58.49)   # 54.78 + 3.71
		self.assertEqual(self._gl(si.name, self.ship_account), 45.0)  # unchanged

	def test_t2_out_of_state_keeps_freight_untaxed(self):
		"""T2 — OOS swap leaves an empty template: freight row only, no tax."""
		so = self._so(self._address("SHIPTAX-CA", "California"), 45.0,
			items=[("SHIP-ACC", 1, 664.0, None)])
		self.assertEqual(so.taxes_and_charges, self.oos_template)
		self.assertEqual([t.account_head for t in so.taxes], [self.ship_account])
		self.assertEqual(self._mirror_rows(so), [])
		self.assertEqual(flt(so.total_taxes_and_charges), 45.0)
		self.assertEqual(flt(so.grand_total), 709.0)

	def test_t3_exempt_customer_keeps_freight_only(self):
		"""T3 — the exempt-customer clear strips the freight tax row along with
		the template rows; only the freight row survives."""
		buyer = "_Test ShipTax Exempt Buyer"
		if not frappe.db.exists("Customer", buyer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": buyer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", buyer, "is_tax_exempt", 1)
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 664.0, None)], customer=buyer)
		self.assertFalse(so.taxes_and_charges)
		self.assertEqual([t.account_head for t in so.taxes], [self.ship_account])
		self.assertEqual(self._mirror_rows(so), [])
		self.assertEqual(flt(so.total_taxes_and_charges), 45.0)

	def test_t4_all_exempt_lines_leave_freight_untaxed(self):
		"""T4 — the template row computes $0 on an
		all-exempt-lines order → NO freight tax row (freight follows the
		non-taxable sale, 34 TAC §3.303), and the template row itself
		survives at $0."""
		si = self._si(45.0, items=[(self.svc_item, 1, 100.0, self.exempt_itt)])
		goods = self._goods_tax_rows(si)
		self.assertEqual(len(goods), 1)                  # template row survives …
		self.assertEqual(flt(goods[0].tax_amount), 0.0)  # … at $0
		self.assertEqual(self._mirror_rows(si), [])
		self.assertEqual(flt(si.total_taxes_and_charges), 45.0)
		si.submit()
		self.assertEqual(self._gl(si.name, self.tax_account), 0.0)  # no GL at all

	def test_t5_mixed_order_taxes_full_freight(self):
		"""T5 — mixed taxable + exempt lines: goods tax on the taxable line
		only, but freight taxed IN FULL (Pub 94-113 unseparated shipment —
		no pro-rating)."""
		so = self._so(None, 45.0, items=[
			("SHIP-ACC", 1, 500.0, None),
			(self.svc_item, 1, 25.0, self.exempt_itt),
		])
		goods = self._goods_tax_rows(so)
		self.assertEqual(flt(sum(flt(t.tax_amount) for t in goods), 2), 41.25)
		self._assert_single_mirror(so, rate_text="8.25", amount=3.71)
		self.assertEqual(flt(so.total_taxes_and_charges), 89.96)  # 41.25 + 45 + 3.71
		self.assertEqual(flt(so.grand_total), 614.96)

	def test_t6_woo_online_order_regression_untouched(self):
		"""T6 — hard constraint 1: a web-order revenue SI (Woo already taxed
		goods + freight at checkout) keeps its taxes table field-for-field
		identical across validates — Actual woo tax row + Actual freight row,
		and NEVER a freight tax row (double taxation)."""
		si = self._si(45.0, items=[("SHIP-ACC", 1, 639.0, None)],
			woo_online_order="WOO-ORD-T6-STUB", woo_tax=52.71)

		def snap(doc):
			return [(t.charge_type, t.account_head, flt(t.rate or 0),
				flt(t.tax_amount, 2), t.description, cint(t.row_id or 0))
				for t in doc.taxes]

		first = snap(si)
		self.assertEqual(len(first), 2)  # woo tax row + freight row, nothing else
		self.assertEqual({t.charge_type for t in si.taxes}, {"Actual"})
		self.assertEqual(self._mirror_rows(si), [])
		self.assertEqual(flt(si.total_taxes_and_charges), 97.71)  # 52.71 + 45
		si.save()  # repeated validate must not grow or mutate anything
		self.assertEqual(snap(si), first)
		self.assertEqual(flt(si.total_taxes_and_charges), 97.71)

	def test_t6b_woo_guard_holds_even_with_template_tax_row(self):
		"""T6 defence-in-depth: even a woo SI that somehow carries an
		"On Net Total" sales-tax row (manual desk edit) must NOT grow a
		freight tax row — the explicit woo_online_order guard, not just the
		"mirrors only non-Actual rows" structural property."""
		si = self._si(45.0, items=[("SHIP-ACC", 1, 639.0, None)],
			woo_online_order="WOO-ORD-T6B-STUB",
			extra_taxes=[{"charge_type": "On Net Total",
				"account_head": self.tax_account, "rate": 8.25,
				"description": "TX Sales Tax"}])
		self.assertEqual(self._mirror_rows(si), [])
		self.assertEqual(len(self._freight_rows(si)), 1)

	def test_t7_idempotent_update_and_zero(self):
		"""T7 — repeated validates change nothing; a freight change moves the
		freight tax row in lockstep; zeroing freight removes BOTH rows and the
		template row is never collateral damage."""
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 664.0, None)])

		def snap(doc):
			return [(t.charge_type, t.account_head, flt(t.rate or 0),
				flt(t.tax_amount, 2), cint(t.row_id or 0)) for t in doc.taxes]

		first = snap(so)
		self.assertEqual(len(first), 3)  # template + freight + freight tax
		for _ in range(3):
			so.save()
		self.assertEqual(snap(so), first)
		# freight 45 → 60: freight row and its tax row move together
		so.ffl_shipping_amount = 60
		so.save()
		self.assertEqual(flt(self._freight_rows(so)[0].tax_amount), 60.0)
		self._assert_single_mirror(so, rate_text="8.25", amount=4.95)
		# freight → 0: both rows vanish; the doc matches a never-shipped order
		so.ffl_shipping_amount = 0
		so.save()
		self.assertEqual(self._freight_rows(so), [])
		self.assertEqual(self._mirror_rows(so), [])
		goods = self._goods_tax_rows(so)
		self.assertEqual(len(goods), 1)
		self.assertEqual(goods[0].charge_type, "On Net Total")
		self.assertEqual(flt(goods[0].tax_amount, 2), 54.78)
		self.assertEqual(flt(so.total_taxes_and_charges), 54.78)

	def test_t8_self_heals_missing_mirror_row(self):
		"""T8 — a pre-fix doc state: freight row exactly right, freight tax
		row absent. The "already exactly right" short-circuit must not strand
		it: the next validate heals it."""
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 664.0, None)])
		mirror = self._mirror_rows(so)[0]
		frappe.db.delete("Sales Taxes and Charges", {"name": mirror.name})
		so.reload()
		self.assertEqual(self._mirror_rows(so), [])  # the legacy state
		self.assertEqual(flt(self._freight_rows(so)[0].tax_amount, 2), 45.0)
		so.save()
		self._assert_single_mirror(so, rate_text="8.25", amount=3.71)
		self.assertEqual(flt(so.total_taxes_and_charges), 103.49)

	def test_t13_a_california_store_taxes_the_goods_not_the_freight(self):
		"""The home state decides — the Company's own address,
		not a constant. In California freight is not part of the taxable price, a
		California destination is in-state (taxed) and Texas is out of state."""
		from unittest.mock import patch

		with patch("ffl_core.compliance.interstate_tax.home_state", return_value="CA"):
			so = self._so(self._address("SHIPTAX-CA", "California"), 45.0,
				items=[("SHIP-ACC", 1, 664.0, None)])
			self.assertEqual(so.taxes_and_charges, self.default_template)
			self.assertEqual(flt(self._goods_tax_rows(so)[0].tax_amount, 2), 54.78)
			self.assertEqual(flt(self._freight_rows(so)[0].tax_amount, 2), 45.0)
			self.assertEqual(self._mirror_rows(so), [])
			self.assertEqual(flt(so.total_taxes_and_charges), 99.78)
			tx = self._so(self._address("SHIPTAX-TX", "Texas"), 45.0,
				items=[("SHIP-ACC", 1, 664.0, None)])
			self.assertEqual(tx.taxes_and_charges, self.oos_template)

		# A document saved while the store taxed freight sheds the row on its
		# next save once it does not.
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 664.0, None)])
		self._assert_single_mirror(so, rate_text="8.25", amount=3.71)
		with patch("ffl_core.compliance.interstate_tax.home_state", return_value="CA"):
			so.save()
		self.assertEqual(self._mirror_rows(so), [])
		self.assertEqual(flt(so.total_taxes_and_charges), 99.78)

	def test_t9_return_and_consolidated_skip(self):
		"""T9 — is_return / is_consolidated docs: the hook keeps its hands off
		entirely (freight row AND freight tax row)."""
		from ffl_core.compliance.shipping_charge import apply_shipping_charge
		for flag in ("is_return", "is_consolidated"):
			si = frappe.new_doc("Sales Invoice")
			si.company = self.company
			si.customer = CUSTOMER
			setattr(si, flag, 1)
			si.ffl_shipping_amount = 45.0
			si.append("taxes", {"charge_type": "On Net Total",
				"account_head": self.tax_account, "rate": 8.25,
				"description": "TX Sales Tax", "tax_amount": 10.0})
			before = [(t.charge_type, t.account_head, flt(t.tax_amount)) for t in si.taxes]
			apply_shipping_charge(si)
			after = [(t.charge_type, t.account_head, flt(t.tax_amount)) for t in si.taxes]
			self.assertEqual(after, before, f"{flag} must skip the hook")

	def test_t10_multi_component_template_mirrors_each_row(self):
		"""T10 — forward-looking state+city split: every
		sales-tax row gets its own freight tax row (charge_type "Actual"),
		amount and description rate text each following its source row. The
		repeat save pins that identification covers NON-default-template
		accounts too (the city account is not in the company default template
		— a default-template-only identity would duplicate its row here)."""
		so = self._so(self._address("SHIPTAX-TX10", "Texas"), 45.0,
			items=[("SHIP-ACC", 1, 100.0, None)], template=self.multi_template)
		self.assertEqual(so.taxes_and_charges, self.multi_template)
		self.assertEqual(len(self._freight_rows(so)), 1)
		mirrors = self._mirror_rows(so)
		self.assertEqual(len(mirrors), 2)
		by_account = {m.account_head: m for m in mirrors}
		state, city = by_account[self.tax_account], by_account[self.city_account]
		self.assertEqual(flt(state.tax_amount, 2), 2.81)   # 6.25% × 45 = 2.8125
		self.assertIn("(6.25%)", state.description)
		self.assertEqual(flt(city.tax_amount, 2), 0.9)     # 2.00% × 45
		self.assertIn("(2%)", city.description)
		for m in mirrors:
			self.assertEqual(m.charge_type, "Actual")
			self.assertFalse(m.row_id)
		# 8.25 goods tax (6.25 + 2.00 on 100) + 45 freight + 3.71 freight tax
		self.assertEqual(flt(so.total_taxes_and_charges), 56.96)
		# idempotency across identify: repeat validate must not duplicate the
		# city row (its account only exists on this doc's template rows)
		so.save()
		self.assertEqual(len(self._mirror_rows(so)), 2)
		self.assertEqual(flt(so.total_taxes_and_charges), 56.96)

	def test_t11_trade_in_credit_line_keeps_full_freight_tax(self):
		"""T11 (the OPRA failure anchor) — a 0% ITT NEGATIVE line
		(TRADE-IN-CREDIT's exact shape) must not distort the freight tax.
		Under the abandoned OPRA mechanism the gun line's freight share becomes
		45×(500/300)=75 (> the whole freight!) → 6.19 collected (+67%, no
		nominal rate explains it); the hook-computed Actual row charges
		rate% × freight, period. Goods still tax on the FULL gun price
		(owner's trade-in determination)."""
		so = self._so(None, 45.0, items=[
			("SHIP-ACC", 1, 500.0, None),
			(self.tradein_item, 1, -200.0, self.exempt_itt),
		])
		self.assertEqual(flt(so.net_total), 300.0)
		goods = self._goods_tax_rows(so)
		self.assertEqual(flt(sum(flt(t.tax_amount) for t in goods), 2), 41.25)
		m = self._assert_single_mirror(so, rate_text="8.25", amount=3.71)
		self.assertNotEqual(flt(m.tax_amount, 2), 6.19)  # the proration failure mode
		self.assertEqual(flt(so.total_taxes_and_charges), 89.96)  # 41.25 + 45 + 3.71
		self.assertEqual(flt(so.grand_total), 389.96)

	def test_t12_rate_change_resyncs_freight_tax(self):
		"""T12 (rate-drift self-heal) — when the goods-tax row's rate on
		an existing draft changes (a template master edit reaches a doc only
		through its copied rows, so the doc-row edit IS the template-change
		path), re-validate must recompute the freight tax row and its
		description rate text — the "already exactly right" short-circuit must
		not block the resync. Amount asserted as flt(45×8.5%, 2) per the flt
		discipline: 3.82 under this site's Banker's rounding (a 3.83 would
		presume commercial rounding on the exact .005 midpoint, 3.825); every
		other matrix figure is midpoint-free."""
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 664.0, None)])
		self._assert_single_mirror(so, rate_text="8.25", amount=3.71)
		self._goods_tax_rows(so)[0].rate = 8.5
		so.save()
		self.assertEqual(flt(self._goods_tax_rows(so)[0].tax_amount, 2), 56.44)  # 664 × 8.5%
		expected = flt(45 * 8.5 / 100.0, 2)
		m = self._assert_single_mirror(so, rate_text="8.5", amount=expected)
		self.assertIn("(8.5%)", m.description)
		self.assertEqual(flt(so.total_taxes_and_charges), flt(56.44 + 45 + expected, 2))

	# ------------------------------------------ tax-inclusive freight (A1–A7)
	# ffl_shipping_tax_inclusive=1 means S already holds
	# the sales tax — t = round_half_up(S × r / (100 + R), 2) from S directly,
	# Shipping row = S − Σt. Figures are the acceptance numbers.

	def _assert_inclusive(self, doc, gross, freight, taxes):
		"""Freight row = `freight`, freight tax rows = `taxes` (by rate text),
		and the two always add back to exactly the customer's shipping S."""
		self.assertEqual(flt(self._freight_rows(doc)[0].tax_amount, 2), freight)
		got = {m.description: flt(m.tax_amount, 2) for m in self._mirror_rows(doc)}
		self.assertEqual(got, {f"{SHIPPING_TAX_DESCRIPTION} ({r}%)": t for r, t in taxes.items()})
		self.assertEqual(flt(freight + sum(got.values()), 2), gross)

	def test_i1_a1_in_state_order_taxes_inside_the_shipping(self):
		"""A1 — 899 goods + S=45 at 8.25%: 74.17 / 41.57 / 3.43, grand 1018.17
		(the legacy basis gave 1021.88), Sales Tax Payable credited 77.60; the
		receipt prints Shipping $45.00 (sales tax included), no Shipping Tax
		line, and its lines add up to the Total."""
		si = self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=1)
		self.assertEqual(flt(self._goods_tax_rows(si)[0].tax_amount, 2), 74.17)
		self._assert_inclusive(si, 45.0, 41.57, {"8.25": 3.43})
		self.assertEqual(flt(si.grand_total), 1018.17)
		si.save()  # the short-circuit holds on the inclusive basis too
		self._assert_inclusive(si, 45.0, 41.57, {"8.25": 3.43})
		si.submit()
		self.assertEqual(self._gl(si.name, self.tax_account), 77.6)
		self.assertEqual(self._gl(si.name, self.ship_account), 41.57)

		html = frappe.render_template(
			frappe.db.get_value("Print Format", "Sales Receipt", "html"), {"doc": si})
		self.assertNotIn(SHIPPING_TAX_DESCRIPTION, html)
		self.assertIn("Shipping (sales tax included)", html)
		self.assertIn(si.get_formatted("ffl_shipping_amount"), html)  # same money format
		self.assertNotIn(self._freight_rows(si)[0].get_formatted("tax_amount"), html)
		for banned in ("no sales tax", "tax-free", "we pay your"):
			self.assertNotIn(banned, html.lower())
		# Subtotal + goods tax + S = Total
		self.assertEqual(flt(si.net_total + 74.17 + 45.0, 2), flt(si.grand_total))

		legacy = self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)])
		html = frappe.render_template(
			frappe.db.get_value("Print Format", "Sales Receipt", "html"), {"doc": legacy})
		self.assertIn(f"{SHIPPING_TAX_DESCRIPTION} (8.25%)", html)
		self.assertNotIn("sales tax included", html)

	def test_i2_a2_every_price_point_adds_back_to_s(self):
		"""A2 — six shipping prices, incl. 16.99 (14.99 + 1% × 200): the tax is
		taken from S, never from the net (the net route gives 1.30 → 17.00)."""
		for gross, freight, tax in ((34.99, 32.32, 2.67), (29.99, 27.70, 2.29),
				(24.99, 23.09, 1.90), (30.0, 27.71, 2.29), (60.0, 55.43, 4.57),
				(16.99, 15.70, 1.29)):
			with self.subTest(gross=gross):
				so = self._so(None, gross, items=[("SHIP-ACC", 1, 100.0, None)], inclusive=1)
				self._assert_inclusive(so, gross, freight, {"8.25": tax})

	def test_i3_a3_no_goods_tax_keeps_the_whole_shipping(self):
		"""A3 — semantics Y unchanged: out-of-state, an exempt (resale) buyer,
		and a trade-in that takes the goods tax to 0 all book S in full."""
		buyer = "_Test ShipTax Incl Exempt Buyer"
		if not frappe.db.exists("Customer", buyer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": buyer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", buyer, "is_tax_exempt", 1)
		so = self._so(None, 35.0, items=[("SHIP-ACC", 1, 664.0, None)],
			customer=buyer, inclusive=1)
		self.assertEqual([(t.account_head, flt(t.tax_amount, 2)) for t in so.taxes],
			[(self.ship_account, 35.0)])
		self.assertEqual(flt(so.grand_total), 699.0)

		so = self._so(self._address("SHIPTAX-INCL-TX", "Texas"), 45.0,
			items=[("SHIP-ACC", 1, 300.0, None), ("SHIP-ACC", 1, -300.0, None)], inclusive=1)
		self.assertEqual(so.taxes_and_charges, self.default_template)
		self.assertEqual(self._mirror_rows(so), [])
		self.assertEqual(flt(self._freight_rows(so)[0].tax_amount, 2), 45.0)
		self.assertEqual(flt(so.grand_total), 45.0)

		# Last, and removed after: ERPNext fills an address-less order from the
		# customer's newest address, which would send later tests out of state.
		ca = self._address("SHIPTAX-INCL-CA", "California")
		self.addCleanup(frappe.delete_doc, "Address", ca, force=1, ignore_permissions=True)
		so = self._so(ca, 45.0, items=[("SHIP-ACC", 1, 664.0, None)], inclusive=1)
		self.assertEqual([t.account_head for t in so.taxes], [self.ship_account])
		self.assertEqual(flt(so.grand_total), 709.0)

	def test_i4_a4_mixed_and_trade_in_orders_back_out_the_full_shipping(self):
		"""A4 — a 0% service line does not prorate the freight tax; a taxable
		trade-in lowers the goods tax (49.42) but not the shipping split."""
		tx = self._address("SHIPTAX-INCL-TX", "Texas")
		so = self._so(tx, 45.0, items=[("SHIP-ACC", 1, 899.0, None),
			(self.svc_item, 1, 25.0, self.exempt_itt)], inclusive=1)
		self._assert_inclusive(so, 45.0, 41.57, {"8.25": 3.43})
		so = self._so(tx, 45.0, items=[("SHIP-ACC", 1, 899.0, None),
			("SHIP-ACC", 1, -300.0, None)], inclusive=1)
		self.assertEqual(flt(sum(flt(t.tax_amount) for t in self._goods_tax_rows(so)), 2), 49.42)
		self._assert_inclusive(so, 45.0, 41.57, {"8.25": 3.43})
		self.assertEqual(flt(so.grand_total), 693.42)

	def test_i5_a5_split_template_rounds_each_row_from_s(self):
		"""A5 — state 6.25 + city 2.00: each row from S over the SUMMED rate."""
		addr = self._address("SHIPTAX-I5", "Texas")
		so = self._so(addr, 45.0, items=[("SHIP-ACC", 1, 100.0, None)],
			template=self.multi_template, inclusive=1)
		self._assert_inclusive(so, 45.0, 41.57, {"6.25": 2.60, "2": 0.83})
		so = self._so(addr, 29.99, items=[("SHIP-ACC", 1, 100.0, None)],
			template=self.multi_template, inclusive=1)
		self._assert_inclusive(so, 29.99, 27.71, {"6.25": 1.73, "2": 0.55})
		so.save()  # repeat validate: no churn, no duplicate city row
		self._assert_inclusive(so, 29.99, 27.71, {"6.25": 1.73, "2": 0.55})

	def test_i6_a6_the_basis_rides_the_document(self):
		"""A6 — the flag copies with the document: a legacy invoice amends and
		returns on 45.00 + 3.71, an inclusive one on 41.57 + 3.43, whatever the
		switch says now; SO → SI mapping carries it too."""
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		for inclusive, freight, tax in ((0, 45.0, 3.71), (1, 41.57, 3.43)):
			with self.subTest(inclusive=inclusive):
				si = self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=inclusive)
				si.submit()
				ret = make_return_doc("Sales Invoice", si.name)
				self.assertEqual(ret.ffl_shipping_tax_inclusive, inclusive)
				self.assertEqual(flt(self._freight_rows(ret)[0].tax_amount, 2), -freight)
				self.assertEqual(flt(self._mirror_rows(ret)[0].tax_amount, 2), -tax)
				if inclusive:
					# The credit note's receipt prints the shipping NEGATIVE, like
					# every other line, so its lines still add up to its Total.
					html = frappe.render_template(frappe.db.get_value(
						"Print Format", "Sales Receipt", "html"), {"doc": ret})
					self.assertIn("Shipping (sales tax included)", html)
					self.assertIn(frappe.utils.fmt_money(-45.0, currency=ret.currency), html)
					self.assertNotIn(SHIPPING_TAX_DESCRIPTION, html)
				si.cancel()
				amended = frappe.copy_doc(si)
				amended.docstatus = 0
				amended.amended_from = si.name
				amended.insert(ignore_permissions=True)
				self.assertEqual(amended.ffl_shipping_tax_inclusive, inclusive)
				self._assert_inclusive(amended, freight + tax, freight, {"8.25": tax})

		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=1)
		so.submit()
		self.assertEqual(make_sales_invoice(so.name).ffl_shipping_tax_inclusive, 1)

	def test_i8_exempt_clear_puts_the_whole_shipping_back(self):
		"""customer_tax_exempt.clear_tax_keep_freight on an inclusive order that
		was taxed first (41.57 + 3.43) and then made exempt within one validate:
		the freight tax rows go and the Shipping row is the whole S again —
		re-derived by apply_shipping_charge, not patched in place."""
		from ffl_core.compliance.customer_tax_exempt import clear_tax_keep_freight
		buyer = "_Test ShipTax Incl Turns Exempt"
		if not frappe.db.exists("Customer", buyer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": buyer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", buyer, "is_tax_exempt", 0)
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 899.0, None)],
			customer=buyer, inclusive=1)
		self._assert_inclusive(so, 45.0, 41.57, {"8.25": 3.43})

		from unittest.mock import patch
		with patch.object(so, "calculate_taxes_and_totals",
				wraps=so.calculate_taxes_and_totals) as calc:
			clear_tax_keep_freight(so)  # the direct call (manual_order's pre-save path)
		self.assertEqual(calc.call_count, 1)  # the freight rebuild's own recalc, no second one
		self.assertEqual([(t.account_head, flt(t.tax_amount, 2)) for t in so.taxes],
			[(self.ship_account, 45.0)])
		self.assertEqual(flt(so.grand_total), 944.0)

		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 899.0, None)],
			customer=buyer, inclusive=1)
		frappe.db.set_value("Customer", buyer, "is_tax_exempt", 1)
		so.save()  # the validate hook path: taxed by the earlier hooks, then cleared
		self.assertFalse(so.taxes_and_charges)
		self.assertEqual([(t.account_head, flt(t.tax_amount, 2)) for t in so.taxes],
			[(self.ship_account, 45.0)])
		self.assertEqual(flt(so.grand_total), 944.0)

	def test_i9_new_documents_take_the_setting_continuations_keep_theirs(self):
		"""r2 #1 — before_insert stamp_shipping_tax_basis: a desk/API-built SO or
		SI (nothing pinned) takes FFL Settings; a desk Duplicate takes it too;
		an amendment, a return and an SO → SI mapping keep the copied value; a
		channel mirror stays 0."""
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		def desk_si(**extra):
			si = frappe.new_doc("Sales Invoice")
			si.update({"company": self.company, "customer": CUSTOMER,
				"currency": frappe.get_cached_value("Company", self.company, "default_currency"),
				"conversion_rate": 1, "debit_to": self._receivable(),
				"due_date": frappe.utils.nowdate(), "update_stock": 0,
				"ffl_shipping_amount": 45, **extra})
			si.append("items", {"item_code": "SHIP-ACC", "qty": 1, "rate": 899})
			si.flags.ignore_permissions = True
			if extra.get("woo_online_order"):
				si.flags.ignore_links = True
			return si.insert()

		for setting in (1, 0):
			with self.subTest(setting=setting):
				self.set_single("FFL Settings", "shipping_price_includes_tax", setting)
				so = frappe.new_doc("Sales Order")
				so.update({"company": self.company, "customer": CUSTOMER,
					"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
					"ffl_shipping_amount": 45})
				so.append("items", {"item_code": "SHIP-ACC", "qty": 1, "rate": 899})
				so.insert(ignore_permissions=True)
				self.assertEqual(so.ffl_shipping_tax_inclusive, setting)
				self.assertEqual(desk_si().ffl_shipping_tax_inclusive, setting)
				self.assertEqual(
					desk_si(woo_online_order="WOO-ORD-I9-STUB").ffl_shipping_tax_inclusive, 0)

		# Continuations keep the value they carry, whatever the setting is now.
		self.set_single("FFL Settings", "shipping_price_includes_tax", 1)
		src = self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=0)
		src.submit()
		self.set_single("FFL Settings", "shipping_price_includes_tax", 0)
		dup = frappe.copy_doc(self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=1))
		dup.insert(ignore_permissions=True)  # a Duplicate is a new sale: today's setting
		self.assertEqual(dup.ffl_shipping_tax_inclusive, 0)
		self.set_single("FFL Settings", "shipping_price_includes_tax", 1)
		ret = make_return_doc("Sales Invoice", src.name)
		ret.insert(ignore_permissions=True)
		self.assertEqual(ret.ffl_shipping_tax_inclusive, 0)
		src.cancel()
		amended = frappe.copy_doc(src)
		amended.docstatus = 0
		amended.amended_from = src.name
		amended.insert(ignore_permissions=True)
		self.assertEqual(amended.ffl_shipping_tax_inclusive, 0)
		so = self._so(None, 45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=0)
		so.submit()
		si = make_sales_invoice(so.name)
		si.insert(ignore_permissions=True)
		self.assertEqual(si.ffl_shipping_tax_inclusive, 0)
		self.assertEqual(flt(self._mirror_rows(si)[0].tax_amount, 2), 3.71)

	def test_i10_receipt_finds_rows_by_account_not_wording(self):
		"""r2 #4 — the receipt identifies the freight row by its account and the
		freight tax rows by the hook's triple: a renamed freight row still prints
		the customer-paid 45.00 with the tax-included note and no tax line."""
		si = self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=1)
		self._freight_rows(si)[0].description = "Freight and handling"
		html = frappe.render_template(
			frappe.db.get_value("Print Format", "Sales Receipt", "html"), {"doc": si})
		self.assertIn("Freight and handling (sales tax included)", html)
		self.assertIn(si.get_formatted("ffl_shipping_amount"), html)
		self.assertNotIn(SHIPPING_TAX_DESCRIPTION, html)
		self.assertNotIn(self._freight_rows(si)[0].get_formatted("tax_amount"), html)

	def test_i11_item_tax_rates_follow_item_tax_templates(self):
		"""r2 #5 — the preview's per-item rate: the template rate for a plain
		item, 0 for an item whose Item Tax Template exempts it."""
		from ffl_core.api.manual_order import item_tax_rates
		rates = item_tax_rates(["SHIP-ACC", self.svc_item, "NO-SUCH-ITEM"], self.company)
		self.assertEqual(rates, {"SHIP-ACC": 8.25, self.svc_item: 0.0})

	def test_i7_a7_amend_to_no_shipping_drops_both_rows(self):
		"""A7 — cancel + amend with S → 0: no Shipping / Shipping Tax rows; the
		cancellation debits Sales Tax Payable for the 3.43 with the goods tax."""
		si = self._si(45.0, items=[("SHIP-ACC", 1, 899.0, None)], inclusive=1)
		si.submit()
		si.cancel()
		reversed_tax = frappe.get_all("GL Entry", filters={"voucher_no": si.name,
			"account": self.tax_account, "debit": [">", 0]}, pluck="debit")
		self.assertEqual(flt(sum(reversed_tax), 2), 77.6)
		amended = frappe.copy_doc(si)
		amended.docstatus = 0
		amended.amended_from = si.name
		amended.ffl_shipping_amount = 0
		amended.insert(ignore_permissions=True)
		self.assertEqual(self._freight_rows(amended), [])
		self.assertEqual(self._mirror_rows(amended), [])
		self.assertEqual(flt(amended.grand_total), 973.17)  # 899 + 74.17


if __name__ == "__main__":
	unittest.main()
