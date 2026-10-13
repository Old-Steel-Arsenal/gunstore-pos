"""Inventory Receipts report — every inbound path lands in the right class with
the right source, category and cost; internal moves and custody stay out unless
asked for; the three views agree with each other and with the stock ledger.

Fixtures post on today's date on the shared dev DB, so assertions locate OUR
vouchers by name rather than counting everything in the window — except the
identity test, which compares the report to the ledger under the same filter.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, today

from ffl_core.api import firearm_intake
from ffl_core.api.item_admin import add_stock
from ffl_core.api.receive_goods import create_receive
from ffl_core.ffl_core.report.inventory_receipts import inventory_receipts as rpt
from ffl_core.firearm import serialized_receipt_row
from ffl_core.tests.test_receive_goods import _ensure_item, _ensure_supplier, _ensure_supplier_group
from ffl_core.warehouses import default_stock_warehouse
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

SUPPLIER = "IR Acme Distribution"   # Supplier names ARE supplier_name
MISC_GROUP = "IR Misc Root"


def _ensure_group(name, parent):
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": name,
			"parent_item_group": parent, "is_group": 0}).insert(ignore_permissions=True)


def _ensure_customer(name):
	if not frappe.db.exists("Customer", name):
		frappe.get_doc({
			"doctype": "Customer", "customer_name": name,
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
		}).insert(ignore_permissions=True)
	return name


class InventoryReceiptsReport(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = (frappe.new_doc("Purchase Receipt").company
			or frappe.db.get_single_value("Global Defaults", "default_company"))
		cls.warehouse = default_stock_warehouse(cls.company) or frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0, "disabled": 0,
				"consignment_dealer": ("is", "not set")}, pluck="name", order_by="creation asc", limit=1)[0]
		_ensure_supplier_group("Distributor")
		_ensure_supplier(SUPPLIER, is_ffl_dealer=1, ffl_number="1-77-777-77-7X-77790",
			ez_check_status="Verified")
		_ensure_group("Handgun", "Firearm")          # the legacy leaf, kept under Firearm
		_ensure_group("Used Firearm", "All Item Groups")  # retired legacy root, still counted
		_ensure_group(MISC_GROUP, "All Item Groups")  # a top-level group that is none of ours
		_ensure_item("IR-PISTOL", item_group="Pistol", has_serial_no=1)
		_ensure_item("IR-CONSIGN", item_group="Pistol", has_serial_no=1)
		_ensure_item("IR-AMMO", item_group="Rifle Ammo", has_serial_no=0)
		_ensure_item("IR-OPTIC", item_group="Optic", has_serial_no=0)
		_ensure_item("IR-RIMFIRE", item_group="Rimfire Ammo", has_serial_no=0)
		# Legacy-leaf firearms. Every firearm-group Item is serial-tracked (the
		# Item hook forces has_serial_no=1), so these receive through the
		# serialised path below rather than add_stock.
		_ensure_item("IR-HANDGUN-NS", item_group="Handgun", has_serial_no=1)
		_ensure_item("IR-USED-NS", item_group="Used Firearm", has_serial_no=1)
		_ensure_item("IR-MISC", item_group=MISC_GROUP, has_serial_no=0)
		cls.customer = _ensure_customer("IR Return Customer")

	def setUp(self) -> None:
		super().setUp()
		p = patch.object(FastBoundClient, "from_settings",
			side_effect=FastBoundConfigError("disabled in report tests"))
		p.start()
		self.addCleanup(p.stop)
		self._tag = self._testMethodName[-12:].replace("_", "")

	# ------------------------------------------------------------ builders

	def _run(self, view="Units", **filters):
		f = {"from_date": today(), "to_date": today(), "view": view}
		f.update(filters)
		return rpt.execute(f)

	def _units(self, **filters):
		return [r for r in self._run("Units", **filters)[1] if not r.get("is_total")]

	def _mine(self, voucher_no, **filters):
		return [r for r in self._units(**filters) if r["voucher_no"] == voucher_no]

	def _receive(self, **over):
		base = {"supplier": SUPPLIER, "source": "Distributor", "acquisition_type": "Purchase",
			"warehouse": self.warehouse, "items": []}
		base.update(over)
		res = create_receive(json.dumps(base))
		self.assertTrue(res["ok"], res)
		return res["name"]

	def _firearm_line(self, serials, rate=500, item_code="IR-PISTOL"):
		return {"item_code": item_code, "qty": len(serials), "rate": rate, "serials": serials,
			"sell_price": 750, "manufacturer": "Glock", "model_name": "19",
			"firearm_type": "Pistol", "caliber": "9mm"}

	def _material_receipt(self, item_code, qty, rate, *, serial=None, posting_date=None, warehouse=None):
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.company,
			"items": [serialized_receipt_row(item_code=item_code, qty=qty,
				warehouse=warehouse or self.warehouse, basic_rate=rate, serial=serial)],
		})
		if posting_date:
			se.set_posting_time = 1
			se.posting_date = posting_date
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return se.name

	def _intake(self, source_type, serial):
		res = firearm_intake.create_intake({
			"serial": serial, "source_type": source_type, "source_name": "Jane Intake",
			"source_id_type": "Driver License", "source_id_number": f"DL-{serial}",
			"manufacturer": "Smith & Wesson", "model_name": "Model 19", "caliber": ".357 Magnum",
			"firearm_type": "Revolver", "acquisition_cost": 350, "condition_grade": "Excellent",
		})
		return frappe.db.get_value("Stock Ledger Entry",
			{"serial_no": ("like", f"%{serial}%"), "is_cancelled": 0, "actual_qty": (">", 0)}, "voucher_no") \
			or frappe.db.get_value("Serial and Batch Entry", {"serial_no": serial, "qty": (">", 0)}, "voucher_no")

	# ------------------------------------------------------------ purchases

	def test_purchase_receipt_explodes_serials_and_keeps_ammo_as_one_line(self):
		sn = [f"IR-P1-{self._tag}", f"IR-P2-{self._tag}"]
		pr = self._receive(items=[self._firearm_line(sn, rate=500),
			{"item_code": "IR-AMMO", "qty": 20, "rate": 0.5}])
		rows = self._mine(pr)
		guns = sorted((r for r in rows if r["category"] == "Firearm"), key=lambda r: r["serial_no"])
		self.assertEqual([r["serial_no"] for r in guns], sn)
		for g in guns:
			self.assertEqual(g["receipt_class"], "Purchase")
			self.assertEqual(g["source"], SUPPLIER)
			self.assertEqual((flt(g["qty"]), flt(g["cost"])), (1.0, 500.0))
			self.assertTrue(g["acquisition"], "firearm unit must link its FFL Acquisition")
			self.assertEqual(g["flags"], "")
		ammo = [r for r in rows if r["category"] == "Ammunition"]
		self.assertEqual(len(ammo), 1)
		self.assertEqual((flt(ammo[0]["qty"]), flt(ammo[0]["cost"])), (20.0, 10.0))
		self.assertIsNone(ammo[0]["serial_no"])

	def test_receipts_tree_nests_lines_and_collapses_multi_serial_units(self):
		sn = [f"IR-T1-{self._tag}", f"IR-T2-{self._tag}"]
		pr = self._receive(items=[self._firearm_line(sn), {"item_code": "IR-AMMO", "qty": 5, "rate": 1}])
		rows = self._run("Receipts")[1]
		vrow = next(r for r in rows if r.get("row_kind") == "voucher" and r["voucher_no"] == pr)
		self.assertEqual(vrow["indent"], 0)
		self.assertEqual((flt(vrow["qty"]), flt(vrow["cost"])), (7.0, 1005.0))
		self.assertEqual(vrow["item_name"], "2 items")
		self.assertEqual(vrow["category"], "Mixed")
		lines = [r for r in rows if r.get("row_kind") == "line" and r["parent_row"] == vrow["row_key"]]
		self.assertEqual(len(lines), 2)
		gun_line = next(l for l in lines if l["item_code"] == "IR-PISTOL")
		self.assertEqual(gun_line["indent"], 1)
		self.assertEqual(gun_line.get("collapse_children"), 1)
		units = [r for r in rows if r.get("row_kind") == "unit" and r["parent_row"] == gun_line["row_key"]]
		self.assertEqual(sorted(u["serial_no"] for u in units), sn)
		self.assertTrue(all(u["indent"] == 2 and u["voucher_no"] is None for u in units))
		ammo_line = next(l for l in lines if l["item_code"] == "IR-AMMO")
		self.assertFalse(ammo_line.get("collapse_children"))
		self.assertFalse([r for r in rows if r.get("parent_row") == ammo_line["row_key"]])

	def test_single_line_single_unit_receipt_is_one_row(self):
		pr = self._receive(items=[self._firearm_line([f"IR-S1-{self._tag}"])])
		rows = [r for r in self._run("Receipts")[1] if r["voucher_no"] == pr or r.get("parent_row") == f"v:{pr}"]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["serial_no"], f"IR-S1-{self._tag}")
		self.assertEqual(rows[0]["item_code"], "IR-PISTOL")

	def test_individual_seller_is_the_source_not_the_placeholder_supplier(self):
		seller = _ensure_customer("IR Jane Seller")
		pr = self._receive(supplier=None, acquisition_source="Individual", source="Individual",
			seller_customer=seller, seller_first_name="Jane", seller_last_name="Seller", id_type="Driver License",
			id_number=f"DL-{self._tag}", seller_address_line1="12 Receive Rd", seller_city="Springfield",
			seller_state="TX", seller_zip="75025", seller_payment_method="Cash",
			items=[self._firearm_line([f"IR-I1-{self._tag}"], rate=250)])
		row = self._mine(pr)[0]
		self.assertEqual(row["receipt_class"], "Purchase")
		self.assertEqual(row["source"], "IR Jane Seller")
		self.assertEqual(flt(row["cost"]), 250.0)

	def test_trade_in_source_on_a_purchase_receipt(self):
		pr = self._receive(source="Individual (trade-in)", items=[self._firearm_line([f"IR-TR-{self._tag}"], rate=300)])
		self.assertEqual(self._mine(pr)[0]["receipt_class"], "Trade-in")

	def test_consignment_receipt_is_zero_cost_without_flags(self):
		consignor = _ensure_customer("IR Consignor Jane")
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt", "supplier": SUPPLIER, "company": self.company,
			"ffl_acquisition_type": "Consignment", "ffl_acquisition_source": "Individual",
			"ffl_seller_customer": consignor, "ffl_seller_id_type": "Driver License",
			"ffl_seller_id_number": f"DL-C-{self._tag}",
			"items": [{"item_code": "IR-CONSIGN", "qty": 1, "rate": 0, "warehouse": self.warehouse,
				"serial_no": f"IR-C1-{self._tag}", "use_serial_batch_fields": 1}],
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		row = self._mine(pr.name)[0]
		self.assertEqual(row["receipt_class"], "Consignment")
		self.assertEqual(flt(row["cost"]), 0.0)
		self.assertEqual(row["source"], "IR Consignor Jane")
		self.assertEqual(row["flags"], "")

	# ------------------------------------------------------------ intake / other vouchers

	def test_intake_and_trade_in_stock_entries(self):
		intake = self._intake("Individual", f"IR-IN-{self._tag}")
		trade = self._intake("Trade-in", f"IR-TI-{self._tag}")
		a = self._mine(intake)[0]
		self.assertEqual(a["receipt_class"], "Intake")
		self.assertEqual(a["source"], "Jane Intake · Individual")
		self.assertEqual(flt(a["cost"]), 350.0)
		self.assertTrue(a["acquisition"])
		self.assertEqual(a["flags"], "")
		self.assertEqual(self._mine(trade)[0]["receipt_class"], "Trade-in")

	def test_customer_return_and_stock_reconciliation(self):
		# A fresh item so the moving average is exactly the receipt rate.
		_ensure_item("IR-OPTIC-RET", item_group="Optic", has_serial_no=0)
		add_stock("IR-OPTIC-RET", 5, warehouse=self.warehouse, rate=40)
		si = frappe.get_doc({
			"doctype": "Sales Invoice", "customer": self.customer, "company": self.company,
			"update_stock": 1, "set_warehouse": self.warehouse,
			"items": [{"item_code": "IR-OPTIC-RET", "qty": 2, "rate": 99, "warehouse": self.warehouse}],
		})
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		ret = make_return_doc("Sales Invoice", si.name)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		row = self._mine(ret.name)[0]
		self.assertEqual(row["receipt_class"], "Return")
		self.assertEqual(row["source"], "IR Return Customer")
		self.assertEqual(flt(row["qty"]), 2.0)
		self.assertEqual(flt(row["cost"]), 80.0)   # the units come back at their valuation

		_ensure_item("IR-OPTIC-ADJ", item_group="Optic", has_serial_no=0)
		sr = frappe.get_doc({
			"doctype": "Stock Reconciliation", "purpose": "Stock Reconciliation", "company": self.company,
			"items": [{"item_code": "IR-OPTIC-ADJ", "warehouse": self.warehouse, "qty": 3, "valuation_rate": 10}],
		})
		sr.flags.ignore_permissions = True
		sr.insert()
		sr.submit()
		row = self._mine(sr.name)[0]
		self.assertEqual(row["receipt_class"], "Adjustment")
		self.assertEqual((flt(row["qty"]), flt(row["cost"])), (3.0, 30.0))

	def test_material_transfer_hidden_unless_included_or_selected(self):
		add_stock("IR-OPTIC", 2, warehouse=self.warehouse, rate=40)
		abbr = frappe.db.get_value("Company", self.company, "abbr")
		target = f"IR Transfer WH - {abbr}"
		if not frappe.db.exists("Warehouse", target):
			frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "IR Transfer WH",
				"company": self.company}).insert(ignore_permissions=True)
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Transfer", "company": self.company,
			"items": [{"item_code": "IR-OPTIC", "qty": 1, "s_warehouse": self.warehouse, "t_warehouse": target}],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		self.assertEqual(self._mine(se.name), [])
		shown = self._mine(se.name, include_transfers=1)
		self.assertEqual(shown[0]["receipt_class"], "Transfer")
		self.assertEqual(shown[0]["source"], f"From {self.warehouse}")
		self.assertEqual(self._mine(se.name, receipt_class="Transfer")[0]["voucher_no"], se.name)

	def test_custody_receipt_hidden_unless_included(self):
		customer = _ensure_customer("IR Transfer Customer")
		pr = self._receive(is_ffl_transfer=1, transfer_for_customer=customer, acquisition_type="FFL Transfer",
			items=[self._firearm_line([f"IR-CU-{self._tag}"], rate=0)])
		self.assertEqual(self._mine(pr), [])
		row = self._mine(pr, include_custody=1)[0]
		self.assertEqual(row["receipt_class"], "Custody")
		self.assertEqual(row["source"], "IR Transfer Customer")
		self.assertEqual(flt(row["cost"]), 0.0)
		self.assertEqual(row["flags"], "")

	# ------------------------------------------------------------ category / dates / flags

	def test_category_rollup_through_the_item_group_tree(self):
		expect = {"IR-RIMFIRE": "Ammunition", "IR-HANDGUN-NS": "Firearm", "IR-USED-NS": "Firearm",
			"IR-MISC": "Other", "IR-OPTIC": "Accessory"}
		vouchers = {
			code: (self._material_receipt(code, 1, 5, serial=f"{code}-{self._tag}")
				if cat == "Firearm"
				else add_stock(code, 1, warehouse=self.warehouse, rate=5)["name"])
			for code, cat in expect.items()
		}
		for code, cat in expect.items():
			row = self._mine(vouchers[code])[0]
			self.assertEqual(row["category"], cat, code)
			self.assertEqual(row["receipt_class"], "Intake")
		self.assertEqual(self._mine(vouchers["IR-MISC"], category="Firearm"), [])
		self.assertEqual(len(self._mine(vouchers["IR-MISC"], category="Other")), 1)

	def test_date_window_and_cancelled_entries(self):
		yesterday = add_days(today(), -1)
		back = self._material_receipt("IR-OPTIC", 1, 40, posting_date=yesterday)
		self.assertEqual(self._mine(back), [])
		self.assertEqual(len(self._mine(back, from_date=yesterday)), 1)
		gone = self._material_receipt("IR-OPTIC", 1, 40)
		self.assertEqual(len(self._mine(gone)), 1)
		frappe.get_doc("Stock Entry", gone).cancel()
		self.assertEqual(self._mine(gone), [])

	def test_flags_no_cost_and_missing_ad(self):
		penny = self._material_receipt("IR-OPTIC", 1, 0.01)   # the FastBound-backfill placeholder
		self.assertEqual(self._mine(penny)[0]["flags"], "No cost")
		bare = self._material_receipt("IR-PISTOL", 1, 400, serial=f"IR-NOAD-{self._tag}")
		row = self._mine(bare)[0]
		self.assertIsNone(row["acquisition"])
		self.assertEqual(row["flags"], "No A&D")

	def test_serial_cost_correction_is_a_revaluation_not_a_receipt(self):
		from ffl_core.api.cost_correction import correct_serial_cost
		serial = f"IR-RV-{self._tag}"
		self._receive(items=[self._firearm_line([serial], rate=10)])
		res = correct_serial_cost(serial, 400, "report test: fix the intake cost")
		sr = res["stock_reconciliation"]
		rows = self._mine(sr)
		self.assertEqual(len(rows), 1, rows)   # the −1 leg is folded, never a row of its own
		row = rows[0]
		self.assertEqual(row["receipt_class"], "Revaluation")
		self.assertEqual(row["serial_no"], serial)
		self.assertEqual(flt(row["qty"]), 0.0)
		self.assertAlmostEqual(flt(row["cost"]), 390.0, places=2)   # 400 − 10: only the difference
		self.assertEqual(row["source"], "Cost correction")
		self.assertEqual(row["flags"], "")   # neither No cost nor No A&D — the gun was already on the books
		summary = self._run("Summary")[1]
		self.assertTrue(any(r.get("row_kind") == "class" and r["group"] == "Revaluation"
			and r["parent_row"] == "c:Firearm" for r in summary))
		# Selecting the type shows it; selecting Adjustment does not.
		self.assertEqual(len(self._mine(sr, receipt_class="Revaluation")), 1)
		self.assertEqual(self._mine(sr, receipt_class="Adjustment"), [])

	def test_plain_item_value_only_reconciliation_is_a_revaluation(self):
		_ensure_item("IR-OPTIC-VAL", item_group="Optic", has_serial_no=0)
		add_stock("IR-OPTIC-VAL", 2, warehouse=self.warehouse, rate=10)
		sr = frappe.get_doc({
			"doctype": "Stock Reconciliation", "purpose": "Stock Reconciliation", "company": self.company,
			"items": [{"item_code": "IR-OPTIC-VAL", "warehouse": self.warehouse, "qty": 2, "valuation_rate": 15}],
		})
		sr.flags.ignore_permissions = True
		sr.insert()
		sr.submit()
		row = self._mine(sr.name)[0]
		self.assertEqual(row["receipt_class"], "Revaluation")
		self.assertEqual((flt(row["qty"]), flt(row["cost"])), (0.0, 10.0))   # 2 × (15 − 10)
		self.assertEqual(row["flags"], "")

	# ------------------------------------------------------------ identities / filters / perms

	def test_views_agree_with_each_other_and_with_the_ledger(self):
		self._receive(items=[self._firearm_line([f"IR-ID-{self._tag}"], rate=500),
			{"item_code": "IR-AMMO", "qty": 4, "rate": 2}])
		add_stock("IR-OPTIC", 3, warehouse=self.warehouse, rate=40)
		from ffl_core.api.cost_correction import correct_serial_cost
		correct_serial_cost(f"IR-ID-{self._tag}", 650, "identity test: revaluation must net")
		units = self._run("Units")[1]
		receipts = self._run("Receipts")[1]
		summary = self._run("Summary")[1]
		u_total = next(r for r in units if r.get("is_total"))
		r_total = next(r for r in receipts if r.get("is_total"))
		s_total = next(r for r in summary if r.get("is_total"))
		self.assertAlmostEqual(flt(u_total["cost"]), flt(r_total["cost"]), places=2)
		self.assertAlmostEqual(flt(u_total["cost"]), flt(s_total["cost"]), places=2)
		self.assertAlmostEqual(flt(u_total["qty"]), flt(s_total["qty"]), places=3)
		voucher_rows = [r for r in receipts if r.get("row_kind") == "voucher"]
		self.assertAlmostEqual(sum(flt(r["cost"]) for r in voucher_rows), flt(r_total["cost"]), places=2)
		cats = [r for r in summary if r.get("row_kind") == "category"]
		self.assertAlmostEqual(sum(flt(r["cost"]) for r in cats), flt(s_total["cost"]), places=2)
		# Ledger identity for our vouchers: report cost == Σ stock_value_difference.
		mine = {r["voucher_no"] for r in units if not r.get("is_total") and r["voucher_no"]
			and (r["item_code"] or "").startswith("IR-")}
		# Same predicate as the report: +qty rows, plus EVERY Stock Reconciliation
		# row — its −1 legs are netted into the Revaluation units, so the report's
		# cost equals the ledger's full value movement for those vouchers.
		ledger = flt(frappe.db.sql("""SELECT COALESCE(SUM(stock_value_difference), 0)
			FROM `tabStock Ledger Entry` WHERE is_cancelled = 0
			AND (actual_qty > 0 OR voucher_type = 'Stock Reconciliation')
			AND voucher_no IN %(v)s""", {"v": list(mine)})[0][0])
		report = sum(flt(r["cost"]) for r in units if not r.get("is_total") and r["voucher_no"] in mine)
		self.assertAlmostEqual(report, ledger, places=2)
		cards = {c["label"]: c for c in self._run("Units")[4]}
		self.assertAlmostEqual(flt(cards["Cost Received"]["value"]), flt(u_total["cost"]), places=2)
		self.assertIn("Firearms (units)", cards)

	def test_supplier_filter_and_refused_segments(self):
		pr = self._receive(items=[{"item_code": "IR-AMMO", "qty": 1, "rate": 1}])
		self.assertEqual(len(self._mine(pr, supplier=SUPPLIER)), 1)
		self.assertEqual(self._mine(pr, supplier="RG-ACME-FFL"), [])
		for bad in ({"view": "Everything"}, {"category": "Guns"}, {"receipt_class": "Bought"}):
			with self.assertRaises(frappe.ValidationError):
				self._run(**bad)
		with self.assertRaises(frappe.ValidationError):
			self._run(from_date=today(), to_date=add_days(today(), -1))

	def test_report_roles_admit_accountants_not_sales(self):
		self.assertTrue(frappe.db.exists("Report", "Inventory Receipts"),
			"Report record missing — run bench migrate to sync ffl_core reports")
		report = frappe.get_doc("Report", "Inventory Receipts")
		self.assertEqual(report.ref_doctype, "Purchase Receipt")
		self.assertEqual(report.report_type, "Script Report")
		users = {}
		for role in ("Accounts User", "Sales User"):
			email = f"ir-{role.lower().replace(' ', '-')}@example.com"
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": role,
					"roles": [{"role": role}], "send_welcome_email": 0}).insert(ignore_permissions=True)
			users[role] = email
		try:
			frappe.set_user(users["Accounts User"])
			self.assertTrue(report.is_permitted())
			self.assertTrue(frappe.has_permission("Purchase Receipt", "report"))
			frappe.set_user(users["Sales User"])
			self.assertFalse(report.is_permitted())
		finally:
			frappe.set_user("Administrator")
