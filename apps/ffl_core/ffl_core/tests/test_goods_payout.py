"""Paying the private seller of a Receive Goods purchase (two boxes of ammo
bought from an individual for $20 cash; the receipt booked Dr Stock / Cr Stock Received But Not Billed and nothing
recorded the cash, so the drawer read $20 high and SRBNB hung open).

``cash_drawer.record_goods_payout`` books Dr Stock Received But Not Billed / Cr Cash, or Cr the bank for Zelle / Check /
ACH (reference required), once per receipt. Run on the two stand-in stores of test_cash_drawer (Store A-like: Operating Bank
exists; Store B-like: no bank yet). Receipts are built here the way Receive Goods submits them (individual source, the
"Individual Seller" placeholder supplier); the receive API itself is covered in test_receive_goods.SellerPayment.
Nothing commits: every test rolls back to its savepoint.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, flt, getdate, now_datetime, nowdate

from ffl_core import cash_drawer as cd
from ffl_core.api import cash_drawer as api
from ffl_core.tests.test_cash_drawer import CashFixtures, _store_tests


class GoodsFixtures(CashFixtures):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.api.receive_goods import _ensure_individual_supplier

		cls.supplier = _ensure_individual_supplier()
		cls.stores = frappe.db.get_value("Warehouse", {"company": cls.company, "warehouse_name": "Stores"})
		cls.srbnb = frappe.get_cached_value("Company", cls.company, "stock_received_but_not_billed")
		for code, group, serial in (("GP-AMMO", "All Item Groups", 0), ("GP-GUN", "Pistol", 1)):
			if not frappe.db.exists("Item", code):
				frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": group,
					"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": serial, "is_firearm": serial,
					"serial_no_series": f"{code}-.####" if serial else ""}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		# A gun's acquisition is pushed to the bound book on submit; these tests are about money, not FastBound.
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in goods payout tests"))
		p.start()
		self.addCleanup(p.stop)

	# --- helpers ------------------------------------------------------------------------------

	def _purchase(self, lines, *, source="Individual", acquisition_type=None, submit=True, **header):
		"""A Receive Goods purchase from the stand-in seller; ``lines`` are (item, qty, rate) or (item, qty, rate, serials)."""
		if header.get("posting_date"):
			header["set_posting_time"] = 1
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt", "company": self.company, "supplier": self.supplier, "posting_date": nowdate(),
			"set_warehouse": self.stores, "ffl_acquisition_source": source, "ffl_acquisition_type": acquisition_type,
			"ffl_seller_customer": "_Test CD Customer", "ffl_seller_id_type": "Driver License", "ffl_seller_id_number": "DL-GP",
			"items": [{"item_code": code, "qty": qty, "rate": rate, "warehouse": self.stores,
				"serial_no": "\n".join(rest[0]) if rest else None} for code, qty, rate, *rest in lines],
			**header,
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		if submit:
			pr.submit()
		return pr

	def _gun_serial(self):
		return f"GP-{self.abbr}-{frappe.generate_hash(length=8).upper()}"

	def _srbnb_left(self, *vouchers):
		return flt(frappe.db.sql(
			"SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE account = %s AND voucher_no IN %s AND is_cancelled = 0",
			(self.srbnb, vouchers))[0][0], 2)

	def _amend(self, pr, rate=None):
		"""Cancel the receipt and submit its amendment (optionally at another rate on every line), the Desk way."""
		pr.reload()
		pr.cancel()
		amended = frappe.copy_doc(pr)
		amended.amended_from = pr.name
		amended.docstatus = 0
		for row in amended.items:
			row.docstatus = 0
			if rate is not None:
				row.rate = rate
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()
		return amended

	def _amend_line(self, pr, item_code, rate):
		"""Cancel and amend, repricing one item's lines only."""
		pr.reload()
		pr.cancel()
		amended = frappe.copy_doc(pr)
		amended.amended_from = pr.name
		amended.docstatus = 0
		for row in amended.items:
			row.docstatus = 0
			if row.item_code == item_code:
				row.rate = rate
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()
		return amended

	def _cancel_reco(self, name):
		reco = frappe.get_doc("Stock Reconciliation", name)
		reco.flags.ignore_permissions = True
		reco.cancel()

	def _payout_of(self, receipt):
		return frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": receipt, "entry_type": "Goods Payout",
			"status": "Posted"}, ["name", "amount", "payment_method", "memo", "payout_key"], as_dict=True)

	def _as(self, user, fn, *args):
		frappe.set_user(user)
		try:
			return fn(*args)
		finally:
			frappe.set_user("Administrator")


class GoodsCases:
	def test_cash_takes_the_purchase_out_of_the_drawer(self):
		"""The incident, booked: Dr SRBNB 20 / Cr Cash 20, dated today; the drawer's figure drops by 20 and the receipt's
		SRBNB nets to zero."""
		before = self._expected()
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		self.assertEqual((paid.amount, paid.excluded), (20, []))
		self.assertEqual(self._lines(paid.journal_entry), {self.srbnb: (20.0, 0), self.cash: (0, 20.0)})
		self.assertEqual(self._expected(), flt(before - 20, 2))
		self.assertEqual(self._srbnb_left(pr.name, paid.journal_entry), 0)
		row = frappe.db.get_value("Cash Drawer Entry", paid.entry, ["entry_type", "purchase_receipt", "customer", "payout_key",
			"payment_method", "posting_date", "status", "debit_account", "credit_account"], as_dict=True)
		self.assertEqual((row.entry_type, row.purchase_receipt, row.customer, row.payout_key, row.payment_method, row.status),
			("Goods Payout", pr.name, "_Test CD Customer", pr.name, "Cash", "Posted"))
		self.assertEqual(getdate(row.posting_date), getdate(nowdate()))
		self.assertTrue(frappe.db.get_value("Journal Entry", paid.journal_entry, "user_remark").startswith(f"CASH-GOODS {pr.name}"))

	def test_a_backdated_receipt_is_paid_on_the_day_the_money_leaves(self):
		"""The receipt's date can be set back; the payment is dated the day it is made, and the memo keeps
		the receipt's date."""
		back = add_days(nowdate(), -5)
		pr = self._purchase([("GP-AMMO", 2, 10)], posting_date=back)
		self.assertEqual(getdate(pr.posting_date), getdate(back))
		paid = cd.record_goods_payout(pr.name, "Cash")
		row = frappe.db.get_value("Cash Drawer Entry", paid.entry, ["posting_date", "memo"], as_dict=True)
		self.assertEqual(getdate(row.posting_date), getdate(nowdate()))
		self.assertEqual(getdate(frappe.db.get_value("Journal Entry", paid.journal_entry, "posting_date")), getdate(nowdate()))
		self.assertIn(f"Receipt {pr.name} dated {back}", row.memo)
		_opening, rows = cd.log_rows(self.company, self.cash, getdate(), getdate())
		self.assertEqual([flt(r.amount) for r in rows if r.kind == "goods_payout"], [-20.0])

	def test_a_bank_payment_needs_its_reference_and_leaves_the_drawer(self):
		pr = self._purchase([("GP-AMMO", 3, 5)])
		for method, reference in ((None, None), ("Venmo", "V-1"), ("Zelle", None), ("Check", "  ")):
			with self.assertRaises(frappe.ValidationError, msg=f"{method} {reference!r}"):
				cd.record_goods_payout(pr.name, method, reference)
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"purchase_receipt": pr.name}))
		before = self._expected()
		paid = cd.record_goods_payout(pr.name, "Zelle", "ZL-55")
		bank = cd.bank_account(self.company)
		self.assertEqual(self._lines(paid.journal_entry), {self.srbnb: (15.0, 0), bank: (0, 15.0)})
		self.assertEqual(self._expected(), before)
		data = self._as(self.manager, api.get_today, self.company)
		listed = [b for b in data["bank_payouts"] if b["entry"] == paid.entry]
		self.assertEqual(len(listed), 1)
		self.assertEqual((listed[0]["purchase_receipt"], listed[0]["method"], listed[0]["amount"]), (pr.name, "Zelle", 15.0))
		self.assertTrue(listed[0]["can_fix"])
		self.assertIn("ZL-55", listed[0]["detail"])
		# Once a count relies on it, the page no longer offers to change it (the server refuses).
		self._close(self._expected())
		listed = [b for b in self._as(self.manager, api.get_today, self.company)["bank_payouts"] if b["entry"] == paid.entry]
		self.assertFalse(listed[0]["can_fix"])
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, pr.name, "Check", "CHK-1")

	def test_the_page_the_log_and_the_weekly_report_show_a_seller_payment(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		line = next(l for l in self._as(self.manager, api.get_today, self.company)["lines"] if l["entry"] == paid.entry)
		self.assertEqual((line["kind"], line["amount"], line["purchase_receipt"], line["label"]),
			("goods_payout", -20.0, pr.name, cd.KIND_LABELS["goods_payout"]))
		self.assertTrue(line["can_undo"] and line["can_fix"])
		for text in (pr.name, "_Test CD Customer", "(Cash)"):
			self.assertIn(text, line["detail"])
		# The page takes the ways to pay from the server.
		self.assertEqual(self._as(self.manager, api.get_today, self.company)["payout_methods"], list(cd.PAYOUT_METHODS))
		as_clerk = next(l for l in self._as(self.clerk, api.get_today, self.company)["lines"] if l["entry"] == paid.entry)
		self.assertFalse(as_clerk["can_undo"] or as_clerk["can_fix"])
		_opening, rows = cd.log_rows(self.company, self.cash, getdate(), getdate())
		self.assertEqual([flt(r.amount) for r in rows if r.kind == "goods_payout"], [-20.0])
		log = self._as(self.manager, api.get_log, self.company, nowdate(), nowdate())
		self.assertEqual([(l["kind"], l["amount"]) for l in log["lines"] if l["voucher_no"] == paid.journal_entry],
			[("goods_payout", -20.0)])
		_cols, report = self._report()
		today = next(d for d in report if d.get("day") == nowdate())
		self.assertEqual(today["payouts"], -20.0)
		self.assertIn(pr.name, today["payout_detail"])
		self.assertEqual(today["other"], 0)
		self.assertEqual(next(r for r in report if r.get("day") == "Difference")["closing_gl"], 0.0)

	def test_a_receipt_is_paid_once(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		cd.record_goods_payout(pr.name, "Cash")
		with self.assertRaises(frappe.ValidationError):
			cd.record_goods_payout(pr.name, "Zelle", "ZL-1")
		with self.assertRaises(frappe.ValidationError):  # not what the receipt owes
			cd._new_entry(entry_type="Goods Payout", company=self.company, amount=5, payment_method="Cash",
				purchase_receipt=pr.name)
		with self.assertRaises(frappe.UniqueValidationError):  # what it owes, but it is paid already
			cd._new_entry(entry_type="Goods Payout", company=self.company, amount=20, payment_method="Cash",
				purchase_receipt=pr.name)
		self.assertEqual(frappe.db.count("Cash Drawer Entry", {"purchase_receipt": pr.name}), 1)
		self.assertEqual(len(self._jes(f"CASH-GOODS {pr.name}")), 1)

	def test_undo_puts_the_cash_back_until_a_count_relies_on_it(self):
		self._sale(100)
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		with self.assertRaises(frappe.PermissionError):
			self._as(self.clerk, api.undo_entry, paid.entry)
		self._as(self.manager, api.undo_entry, paid.entry)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid.entry, "status"), "Voided")
		self.assertEqual(self._expected(), 100)
		self.assertEqual(self._srbnb_left(pr.name), -20)  # back to what the receipt alone left open
		# The page lists it to record again, for a manager.
		undone = self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"]
		self.assertEqual([(u["purchase_receipt"], u["amount"], u["method"], u["can_fix"]) for u in undone],
			[(pr.name, 20.0, "Cash", True)])
		self.assertEqual(self._as(self.clerk, api.get_today, self.company)["unpaid_goods_receipts"], [])  # r4 #7: managers
		again = self._as(self.manager, api.record_goods_payout, pr.name, "Cash")  # paid after all: booked again
		self.assertEqual(self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"], [])
		self.assertEqual((again.amount, self._expected()), (20, 80))
		self._close(80)
		with self.assertRaises(frappe.ValidationError):
			cd.void_entry(again.entry)
		with self.assertRaises(frappe.ValidationError):  # changing the method is an undo too: refused after the count
			self._as(self.manager, api.record_goods_payout, pr.name, "Zelle", "ZL-2")
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", again.entry, "status"), "Posted")

	def test_a_manager_changes_the_method_in_one_step(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		before = self._expected()
		paid = cd.record_goods_payout(pr.name, "Cash")
		with self.assertRaises(frappe.PermissionError):
			self._as(self.clerk, api.record_goods_payout, pr.name, "Check", "CHK-101")
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, pr.name, "Check", "")
		fixed = self._as(self.manager, api.record_goods_payout, pr.name, "Check", "CHK-101")
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid.entry, "status"), "Voided")
		row = frappe.db.get_value("Cash Drawer Entry", fixed.entry, ["payment_method", "reference", "status"], as_dict=True)
		self.assertEqual((row.payment_method, row.reference, row.status), ("Check", "CHK-101", "Posted"))
		self.assertEqual(self._lines(fixed.journal_entry), {self.srbnb: (20.0, 0), cd.bank_account(self.company): (0, 20.0)})
		self.assertEqual(self._expected(), before)

	def test_blank_purchase_and_individual_types_are_paid_at_receipt(self):
		"""The pay-now types. "Individual" is FastBound's type for a non-licensee: a gun bought
		from a private seller, paid by check, is one -- the bank pays, the drawer does not move."""
		before = self._expected()
		bank = cd.bank_account(self.company)
		for n, acquisition_type in enumerate((None, "Purchase", " individual ", "INDIVIDUAL")):
			pr = self._purchase([("GP-AMMO", 1, 650.00)], acquisition_type=acquisition_type)
			self.assertTrue(cd.receipt_pays_seller(pr), acquisition_type)
			paid = cd.record_goods_payout(pr.name, "Check", f"CHK-{n}")
			self.assertEqual(self._lines(paid.journal_entry), {self.srbnb: (650.00, 0), bank: (0, 650.00)}, acquisition_type)
		self.assertEqual(self._expected(), before)

	def test_only_a_private_sellers_purchase_is_paid_here(self):
		"""Exempt, each on its own: a dealer purchase, a consignor's stock (paid when it sells), a gun in for gunsmithing
		(the customer's), a transfer by its type or its pickup customer, a customer-custody receipt, a type nobody has
		looked at, a draft, a missing receipt."""
		exempt = {
			"dealer": self._purchase([("GP-AMMO", 1, 10)], source="FFL Dealer"),
			"consignment": self._purchase([("GP-AMMO", 1, 10)], acquisition_type="Consignment"),
			"gunsmithing": self._purchase([("GP-AMMO", 1, 10)], acquisition_type="Gunsmithing"),
			"transfer type": self._purchase([("GP-AMMO", 1, 10)], acquisition_type="FFL Transfer"),
			"pickup customer": self._purchase([("GP-AMMO", 1, 10)], acquisition_type="Transfer",
				ffl_transfer_for_customer="_Test CD Customer"),
			"custody box": self._purchase([("GP-AMMO", 1, 10)], is_ffl_transfer=1),
			"unknown type": self._purchase([("GP-AMMO", 1, 10)], acquisition_type="Return"),
			"draft": self._purchase([("GP-AMMO", 1, 10)], submit=False),
		}
		for what, pr in exempt.items():
			self.assertFalse(cd.receipt_pays_seller(pr) and pr.docstatus == 1, what)
		for name in [pr.name for pr in exempt.values()] + ["MAT-PRE-NOT-THERE", None]:
			with self.assertRaises(frappe.ValidationError, msg=name):
				cd.record_goods_payout(name, "Cash")
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"entry_type": "Goods Payout", "company": self.company}))
		self.assertEqual(self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"], [])

	def test_a_gun_on_the_receipt_is_paid_with_it_and_never_twice(self):
		"""Receive Goods puts a private seller's gun into stock against SRBNB like the ammo (an FFL Acquisition, no
		seller payment of its own), so the receipt's payment covers it. The gun is never paid on its own,
		whether the receipt's payment is posted, undone or not made yet."""
		serial = self._gun_serial()
		pr = self._purchase([("GP-GUN", 1, 300, [serial]), ("GP-AMMO", 2, 10)])
		acq = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": pr.name, "docstatus": 1}, "name")
		self.assertTrue(acq)
		for replace in (False, True):  # owed, not paid yet
			with self.assertRaises(frappe.ValidationError):
				cd.record_payout(acq, "Cash", replace=replace)
		before = self._expected()
		paid = cd.record_goods_payout(pr.name, "Cash")
		self.assertEqual((paid.amount, paid.excluded), (320, []))
		self.assertEqual(self._srbnb_left(pr.name, paid.journal_entry), 0)
		with self.assertRaises(frappe.ValidationError):  # paid
			cd.record_payout(acq, "Cash", replace=True)
		cd.void_entry(paid.entry)
		with self.assertRaises(frappe.ValidationError):  # undone
			cd.record_payout(acq, "Zelle")
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"acquisition": acq, "entry_type": "Payout"}))
		self.assertEqual(self._expected(), before)

	def test_a_gun_paid_on_its_own_before_is_left_out_and_named(self):
		"""A gun whose acquisition already carries its own payout (one made before receipts paid their sellers) is left
		out of the receipt's payment, and the entry's memo says which."""
		serial = self._gun_serial()
		pr = self._purchase([("GP-GUN", 1, 300, [serial]), ("GP-AMMO", 2, 10)])
		acq = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": pr.name, "docstatus": 1}, "name")
		gun = cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash",
			acquisition=acq, serial=serial)  # the old way, straight in
		rest = cd.record_goods_payout(pr.name, "Cash")
		self.assertEqual(rest.amount, 20)
		self.assertEqual([(e.serial, e.acquisition, e.entry, e.amount) for e in rest.excluded], [(serial, acq, gun.entry, 300.0)])
		memo = frappe.db.get_value("Cash Drawer Entry", rest.entry, "memo")
		for text in (serial, acq, gun.entry, "$300.00"):
			self.assertIn(text, memo)

	def test_a_gun_paid_on_its_own_counts_as_paid_at_its_line_on_the_month_end_check(self):
		"""The own payout booked again at a corrected net ($320 on a $300 line) still settles the receipt:
		the check counts the gun at its line's rate, as the receipt's payment left it out."""
		serial = self._gun_serial()
		pr = self._purchase([("GP-GUN", 1, 300, [serial]), ("GP-AMMO", 2, 10)])
		acq = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": pr.name, "docstatus": 1}, "name")
		cd._new_entry(entry_type="Payout", company=self.company, amount=320, payment_method="Cash", acquisition=acq,
			serial=serial)
		def unsettled():
			return [r.left for r in cd.unsettled_seller_receipts(pr.posting_date, pr.posting_date) if r.name == pr.name]
		self.assertEqual(unsettled(), [-20])  # the ammo is still owed; the gun counts at its $300 line, not the $320 paid
		cd.record_goods_payout(pr.name, "Cash")
		self.assertEqual(unsettled(), [])

	def test_a_cost_correction_on_a_gun_paid_with_its_receipt_follows_the_payment(self):
		"""The cost dialog sees the receipt's payment; a different amount paid books the difference like a
		gun's own payout (Dr Stock Adjustment / Cr the same cash or bank), linked to the receipt, so changing the method,
		undoing and recording again carry it along."""
		from ffl_core.api import cost_correction

		serial = self._gun_serial()
		pr = self._purchase([("GP-GUN", 1, 300, [serial])])
		paid = cd.record_goods_payout(pr.name, "Cash")
		listed = next(s for s in cost_correction.list_item_serials_for_cost("GP-GUN")["serials"] if s["serial"] == serial)
		self.assertEqual((listed["cash_payout"], listed["goods_payout"]), (True, paid.entry))
		before = self._expected()
		res = cost_correction.correct_serial_cost(serial, 350, "seller was paid 350", payout_was_different=1)
		self.assertTrue(res["payout_adjustment"])
		adjustment = frappe.db.get_value("Cash Drawer Entry", {"journal_entry": res["payout_adjustment"]},
			["name", "entry_type", "amount", "payment_method", "purchase_receipt"], as_dict=True)
		self.assertEqual((adjustment.entry_type, flt(adjustment.amount), adjustment.payment_method, adjustment.purchase_receipt),
			("Payout Adjustment", 50.0, "Cash", pr.name))
		self.assertEqual(self._expected(), flt(before - 50, 2))
		adjustment_account = f"Stock Adjustment - {self.abbr}"
		net = frappe.db.sql("SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE account = %s AND is_cancelled = 0 "
			"AND voucher_no IN %s", (adjustment_account, (res["stock_reconciliation"], res["payout_adjustment"])))[0][0]
		self.assertEqual(flt(net, 2), 0)
		# Change the method: both come back by Zelle.
		fixed = self._as(self.manager, api.record_goods_payout, pr.name, "Zelle", "ZL-9")
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", adjustment.name, "status"), "Voided")
		moved = frappe.get_all("Cash Drawer Entry", filters={"purchase_receipt": pr.name, "status": "Posted"},
			fields=["entry_type", "amount", "payment_method"], order_by="creation")
		self.assertEqual([(m.entry_type, flt(m.amount), m.payment_method) for m in moved],
			[("Goods Payout", 300.0, "Zelle"), ("Payout Adjustment", 50.0, "Zelle")])
		self.assertEqual(self._expected(), flt(before + 300, 2))
		# Undo, then record again: both again.
		self._as(self.manager, api.undo_entry, fixed.entry)
		self.assertFalse(frappe.get_all("Cash Drawer Entry", filters={"purchase_receipt": pr.name, "status": "Posted"}))
		self._as(self.manager, api.record_goods_payout, pr.name, "Cash")
		self.assertEqual(self._expected(), flt(before - 50, 2))

	def test_an_amendment_takes_the_payment_over(self):
		"""Cancel + amend at the same amount moves the standing payment to the amendment (its receipt and
		its once-per-receipt key), with a note; no money moves."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		before = self._expected()
		amended = self._amend(pr)
		row = self._payout_of(amended.name)
		self.assertEqual((row.name, row.payout_key), (paid.entry, amended.name))
		self.assertIn(f"Moved from {pr.name} to {amended.name}", row.memo)
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Cash Drawer Entry", "reference_name": paid.entry}))
		self.assertFalse(self._payout_of(pr.name))
		self.assertEqual(self._expected(), before)
		with self.assertRaises(frappe.ValidationError):  # still once per receipt
			cd.record_goods_payout(amended.name, "Cash")

	def test_an_amendment_at_another_amount_leaves_the_payment_and_says_so(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		frappe.local.message_log = []
		amended = self._amend(pr, rate=12)
		self.assertEqual(self._payout_of(pr.name).name, paid.entry)
		self.assertTrue(any(paid.entry in str(m) for m in frappe.local.message_log))
		unpaid = lambda: [u["purchase_receipt"] for u in self._as(self.manager, api.get_today, self.company)[
			"unpaid_goods_receipts"]]
		self.assertEqual(unpaid(), [])  # still paid on the original: not offered, and refused
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, amended.name, "Cash")
		self._as(self.manager, api.undo_entry, paid.entry)
		self.assertEqual(unpaid(), [amended.name])
		again = self._as(self.manager, api.record_goods_payout, amended.name, "Cash")
		self.assertEqual(again.amount, 24)
		self.assertEqual([r.name for r in cd.receipt_chain(amended.name)], [pr.name, amended.name])  # one walker

	def test_the_same_goods_received_again_are_not_paid_twice(self):
		"""A payment still standing on a cancelled receipt of this seller, for the same amount, refuses
		paying again for a new receipt; another amount is a new purchase."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		cd.record_goods_payout(pr.name, "Cash")
		pr.reload()
		pr.cancel()
		again = self._purchase([("GP-AMMO", 2, 10)])
		with self.assertRaises(frappe.ValidationError):
			cd.record_goods_payout(again.name, "Cash")
		other = self._purchase([("GP-AMMO", 3, 10)])
		self.assertEqual(cd.record_goods_payout(other.name, "Cash").amount, 30)

	def test_cancelling_the_receipt_keeps_the_payment_and_says_how_to_take_it_back(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		before = self._expected()
		frappe.local.message_log = []
		pr.reload()
		pr.cancel()
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid.entry, "status"), "Posted")
		self.assertEqual(self._expected(), before)
		self.assertTrue(any(paid.entry in str(m) and "Amend" in str(m) for m in frappe.local.message_log))
		with self.assertRaises(frappe.ValidationError):  # a cancelled receipt is not paid again
			cd.record_goods_payout(pr.name, "Cash", replace=True)

	# ------------------------------------------------------------------ amendment and undo cases

	def _set_tracking_started(self, when):
		frappe.db.set_single_value("Cash Drawer Settings", "tracking_started", when)

	def _gun_receipt(self, rate=300, extra=()):
		serial = self._gun_serial()
		pr = self._purchase([("GP-GUN", 1, rate, [serial]), *extra])
		acq = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": pr.name, "docstatus": 1}, "name")
		return pr, serial, acq

	def test_r2_3a_a_receipt_never_paid_since_tracking_is_offered_and_paid_once(self):
		"""A pay-now receipt that never had a seller payment (received before payments were booked at
		receive, or outside Receive Goods) is listed and a manager records its first payment; one received before cash
		tracking started stays the accountant's: not listed, refused."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		listed = self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"]
		self.assertEqual([(u["purchase_receipt"], u["amount"], u["method"]) for u in listed], [(pr.name, 20.0, None)])
		first = self._as(self.manager, api.record_goods_payout, pr.name, "Check", "CHK-7")
		self.assertEqual((first.amount, frappe.db.get_value("Cash Drawer Entry", first.entry, "payment_method")), (20, "Check"))
		self.assertEqual(self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"], [])
		older = self._purchase([("GP-AMMO", 1, 10)])
		self._set_tracking_started(add_to_date(now_datetime(), days=1))
		self.assertEqual(self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"], [])
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, older.name, "Cash")
		self.assertFalse(self._payout_of(older.name))

	def test_r2_3b_a_gun_that_had_its_own_payout_is_paid_again_the_old_way(self):
		"""A gun whose own payout (made the old way) was undone is booked again through record_payout,
		as before -- unless the receipt's payment covers it now."""
		pr, serial, acq = self._gun_receipt()
		own = cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash",
			acquisition=acq, serial=serial)
		cd.void_entry(own.entry)
		again = cd.record_payout(acq, "Zelle")
		self.assertEqual((again.amount, frappe.db.get_value("Cash Drawer Entry", again.entry, "status")), (300, "Posted"))
		cd.void_entry(again.entry)
		cd.record_goods_payout(pr.name, "Cash")  # its receipt pays for it now
		with self.assertRaises(frappe.ValidationError):
			cd.record_payout(acq, "Cash")

	def test_r2_4_record_again_carries_the_differences_from_the_receipt_they_were_undone_on(self):
		"""R1 paid, +$50 cost difference on R1; amended to R2 at another amount (the payment stays on R1);
		R1's payment undone; recorded on R2: the $50 comes along, linked to R2 and its own acquisition. (ERPNext refuses
		to cancel R1 while the cost correction's Stock Reconciliation stands, so that is cancelled first, as it says.)"""
		from ffl_core.api import cost_correction

		pr, serial, _acq = self._gun_receipt(extra=[("GP-AMMO", 2, 10)])
		cd.record_goods_payout(pr.name, "Cash")
		res = cost_correction.correct_serial_cost(serial, 350, "seller was paid 350", payout_was_different=1)
		self._cancel_reco(res["stock_reconciliation"])
		amended = self._amend_line(pr, "GP-AMMO", 12)
		self.assertTrue(self._payout_of(pr.name))
		self._as(self.manager, api.undo_entry, self._payout_of(pr.name).name)
		self._as(self.manager, api.record_goods_payout, amended.name, "Cash")
		moved = frappe.get_all("Cash Drawer Entry", filters={"purchase_receipt": amended.name, "status": "Posted"},
			fields=["entry_type", "amount", "acquisition"], order_by="creation")
		own = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": amended.name, "serial": serial, "docstatus": 1}, "name")
		self.assertEqual([(m.entry_type, flt(m.amount), m.acquisition) for m in moved],
			[("Goods Payout", 324.0, None), ("Payout Adjustment", 50.0, own)])

	def test_r2_5_a_cost_correction_after_a_takeover_books_only_the_new_difference(self):
		"""After the amendment takes the payment (and its +$50) over, correcting the cost again books only
		what is new, not the +$50 a second time: the amendment received the gun at 300 again, 300 + 50 are paid, so
		375 owes 25."""
		from ffl_core.api import cost_correction

		pr, serial, _acq = self._gun_receipt()
		cd.record_goods_payout(pr.name, "Cash")
		res = cost_correction.correct_serial_cost(serial, 350, "seller was paid 350", payout_was_different=1)
		self._cancel_reco(res["stock_reconciliation"])  # ERPNext refuses the receipt's cancel while it stands
		amended = self._amend(pr)
		self.assertEqual(self._payout_of(amended.name).payout_key, amended.name)
		own = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": amended.name, "serial": serial, "docstatus": 1}, "name")
		self.assertEqual([(a.acquisition, flt(a.amount)) for a in cd._goods_adjustments(amended.name)], [(own, 50.0)])
		before = self._expected()
		res = cost_correction.correct_serial_cost(serial, 375, "and 25 more", payout_was_different=1)
		self.assertEqual(flt(frappe.db.get_value("Cash Drawer Entry", {"journal_entry": res["payout_adjustment"]}, "amount")), 25)
		self.assertEqual(self._expected(), flt(before - 25, 2))

	def test_r2_6_nothing_is_undone_unless_a_new_payment_is_booked(self):
		"""A receipt that owes nothing now (its only gun was paid on its own after all) refuses a new
		payment and keeps the one recorded; a first payment for such a receipt is refused too."""
		pr, serial, acq = self._gun_receipt()
		paid = cd.record_goods_payout(pr.name, "Cash")
		cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash", acquisition=acq,
			serial=serial)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._as(self.manager, api.record_goods_payout, pr.name, "Zelle", "ZL-1")
		self.assertIn("owes the seller nothing", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid.entry, "status"), "Posted")
		other, other_serial, other_acq = self._gun_receipt()
		cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash", acquisition=other_acq,
			serial=other_serial)
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, other.name, "Cash")

	def test_r2_8_the_cost_dialog_reads_seller_payments_for_the_whole_sku_at_once(self):
		"""One batch for the SKU, never the per-serial lookups or a full receipt load."""
		from ffl_core.api import cost_correction

		paid_pr, paid_serial, _ = self._gun_receipt()
		goods = cd.record_goods_payout(paid_pr.name, "Cash")
		_own_pr, own_serial, own_acq = self._gun_receipt()
		cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash", acquisition=own_acq,
			serial=own_serial)
		_none_pr, none_serial, _ = self._gun_receipt()
		with patch.object(cd, "goods_payout_cover", side_effect=AssertionError("per serial")):
			listed = {s["serial"]: s for s in cost_correction.list_item_serials_for_cost("GP-GUN")["serials"]}
		self.assertEqual((listed[paid_serial]["cash_payout"], listed[paid_serial]["goods_payout"]), (True, goods.entry))
		self.assertEqual((listed[own_serial]["cash_payout"], listed[own_serial]["goods_payout"]), (True, None))
		self.assertEqual((listed[none_serial]["cash_payout"], listed[none_serial]["goods_payout"]), (False, None))

	def test_r2_9_the_unpaid_list_is_one_query_whatever_its_length(self):
		for n in range(3):
			self._purchase([("GP-AMMO", 1, 10 + n)])
		real = frappe.db.sql
		calls = []
		with patch.object(frappe.db, "sql", side_effect=lambda *a, **k: calls.append(a[0]) or real(*a, **k)):
			rows = cd.receipts_to_pay(self.company)
		self.assertEqual(len(rows), 3)
		self.assertEqual(len([c for c in calls if "tabPurchase Receipt" in c]), 1)
		# the receipts, when cash tracking started, and which of them carry a gun paid on its own
		self.assertLessEqual(len(calls), 3)

	# ------------------------------------------------------------------ paid outside the system (before r3)

	def test_a_receipt_paid_outside_the_system_leaves_the_list_and_is_not_paid_again(self):
		"""A purchase paid outside the system (cash with a hand-posted JE, or a check booked in an outside ledger):
		a manager marks the purchase paid outside the system with a note. Nothing is booked, it leaves the
		list, its payment is refused; clearing the mark brings it back."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		unpaid = lambda: [u["purchase_receipt"] for u in self._as(self.manager, api.get_today, self.company)[
			"unpaid_goods_receipts"]]
		self.assertEqual(unpaid(), [pr.name])
		before = self._expected()
		with self.assertRaises(frappe.PermissionError):
			self._as(self.clerk, api.mark_paid_outside, pr.name, "ACC-JV-2026-99001")
		with self.assertRaises(frappe.ValidationError):  # the note is required
			self._as(self.manager, api.mark_paid_outside, pr.name, "  ")
		marked = self._as(self.manager, api.mark_paid_outside, pr.name, "ACC-JV-2026-99001")
		self.assertEqual((marked["note"], marked["by"]), ("ACC-JV-2026-99001", self.manager))
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, cd.PAID_OUTSIDE_FIELD), "ACC-JV-2026-99001")
		comment = frappe.db.get_value("Comment", {"reference_doctype": "Purchase Receipt", "reference_name": pr.name,
			"content": ["like", "%paid outside the system%ACC-JV-2026-99001%"]}, ["owner", "creation"], as_dict=True)
		self.assertEqual(comment.owner, self.manager)
		self.assertEqual(unpaid(), [])
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"purchase_receipt": pr.name}))
		self.assertEqual(self._expected(), before)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._as(self.manager, api.record_goods_payout, pr.name, "Cash")
		self.assertIn("ACC-JV-2026-99001", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):  # marked once
			self._as(self.manager, api.mark_paid_outside, pr.name, "again")
		with self.assertRaises(frappe.PermissionError):
			self._as(self.clerk, api.clear_paid_outside, pr.name)
		self._as(self.manager, api.clear_paid_outside, pr.name)
		self.assertEqual(unpaid(), [pr.name])
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Purchase Receipt", "reference_name": pr.name,
			"content": ["like", "%mark cleared%"]}))
		paid = self._as(self.manager, api.record_goods_payout, pr.name, "Cash")
		self.assertTrue(paid.entry)
		with self.assertRaises(frappe.ValidationError):  # paid in the system now: not marked as paid outside
			self._as(self.manager, api.mark_paid_outside, pr.name, "ACC-JV-2026-99001")

	def test_an_amendment_answers_to_its_originals_paid_outside_mark(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		self._as(self.manager, api.mark_paid_outside, pr.name, "QuickBooks check 1042")
		amended = self._amend(pr)
		# Desk's amend copies the mark (no_copy fields ride along on an amendment); a copy without it still answers
		# to its original's, through the amendment line.
		frappe.db.set_value("Purchase Receipt", amended.name, cd.PAID_OUTSIDE_FIELD, None)
		self.assertEqual(self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"], [])
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, amended.name, "Check", "1042")


	# ------------------------------------------------------------------ amendment edge cases

	def _other_seller(self):
		if not frappe.db.exists("Customer", "_Test GP Other Seller"):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test GP Other Seller",
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)
		return "_Test GP Other Seller"

	def test_r3_1_clearing_the_mark_on_an_amendment_clears_the_whole_line(self):
		"""The mark is honoured on the whole amendment line, so clearing it on the amendment (the only
		submitted receipt, where the form's button is) clears the cancelled original's too: listed and payable again."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		self._as(self.manager, api.mark_paid_outside, pr.name, "QuickBooks check 1042")
		amended = self._amend(pr)
		cleared = self._as(self.manager, api.clear_paid_outside, amended.name)
		self.assertIn(pr.name, cleared["receipts"])
		self.assertFalse(frappe.db.get_value("Purchase Receipt", pr.name, cd.PAID_OUTSIDE_FIELD))
		self.assertEqual([u["purchase_receipt"] for u in self._as(self.manager, api.get_today, self.company)[
			"unpaid_goods_receipts"]], [amended.name])
		self.assertTrue(self._as(self.manager, api.record_goods_payout, amended.name, "Cash").entry)

	def test_r3_2_a_busy_drawer_is_a_plain_refusal(self):
		"""Frappe.db.sql wraps MariaDB's lock-wait timeout in QueryTimeoutError (and a deadlock in
		QueryDeadlockError); both come out as the plain "drawer is busy" refusal, not a server error."""
		for exc in (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
			with patch.object(frappe.db, "sql", side_effect=exc("1205")):
				with self.assertRaises(frappe.ValidationError) as ctx:
					cd.lock_drawer(self.company, wait=5)
			self.assertIn("busy", str(ctx.exception))

	def test_r3_4_an_amendment_to_another_seller_does_not_take_the_payment_over(self):
		"""The same amount is not enough -- an amendment naming another seller leaves the payment (made to
		the first seller) on the cancelled receipt, and says so."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Cash")
		pr.reload()
		pr.cancel()
		amended = frappe.copy_doc(pr)
		amended.amended_from = pr.name
		amended.docstatus = 0
		amended.ffl_seller_customer = self._other_seller()
		for row in amended.items:
			row.docstatus = 0
		amended.flags.ignore_permissions = True
		frappe.local.message_log = []
		amended.insert()
		amended.submit()
		row = frappe.db.get_value("Cash Drawer Entry", paid.entry, ["purchase_receipt", "customer"], as_dict=True)
		self.assertEqual((row.purchase_receipt, row.customer), (pr.name, "_Test CD Customer"))
		self.assertTrue(any(paid.entry in str(m) and "_Test GP Other Seller" in str(m) for m in frappe.local.message_log))

	def test_r3_5_a_receipt_typed_in_desk_for_goods_already_paid_is_refused_on_the_page(self):
		"""R1 paid then cancelled (its payment stands); the same goods typed on a new receipt R2 in Desk.
		"Record payment" on the Cash Drawer page runs the same refusals as a receive: refused, nothing booked."""
		pr = self._purchase([("GP-AMMO", 2, 10)])
		cd.record_goods_payout(pr.name, "Cash")
		pr.reload()
		pr.cancel()
		again = self._purchase([("GP-AMMO", 2, 10)])
		with self.assertRaises(frappe.ValidationError):
			self._as(self.manager, api.record_goods_payout, again.name, "Cash")
		self.assertFalse(self._payout_of(again.name))

	def test_r3_6_the_unpaid_list_shows_what_is_still_owed(self):
		"""A gun paid on its own is not owed again -- a receipt with it and $20 of ammo lists $20, and a
		receipt whose only gun was paid on its own is not listed at all."""
		mixed, mixed_serial, mixed_acq = self._gun_receipt(rate=500, extra=[("GP-AMMO", 1, 20)])
		cd._new_entry(entry_type="Payout", company=self.company, amount=500, payment_method="Cash", acquisition=mixed_acq,
			serial=mixed_serial)
		alone, alone_serial, alone_acq = self._gun_receipt(rate=300)
		cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash", acquisition=alone_acq,
			serial=alone_serial)
		listed = {u["purchase_receipt"]: u["amount"] for u in self._as(self.manager, api.get_today, self.company)[
			"unpaid_goods_receipts"]}
		self.assertEqual(listed.get(mixed.name), 20.0)
		self.assertNotIn(alone.name, listed)

	def test_r3_7_the_dialog_amount_is_what_the_receipt_books(self):
		"""100 rounds at $0.125 -- the dialog's amount is worked out at the receipt's own precisions, so it
		equals what the receipt books."""
		decision = cd.seller_payment_at_receipt({"ffl_acquisition_source": "Individual"}, [(100, 0.125)])
		pr = self._purchase([("GP-AMMO", 100, 0.125)])
		self.assertEqual(decision.amount, cd.goods_payout_due(pr).amount)

	def test_r3_8_a_bank_payment_for_a_receipt_can_be_undone_on_the_page(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		paid = cd.record_goods_payout(pr.name, "Check", "CHK-8")
		bank = [b for b in self._as(self.manager, api.get_today, self.company)["bank_payouts"] if b["entry"] == paid.entry]
		self.assertTrue(bank and bank[0]["can_undo"])
		self._as(self.manager, api.undo_entry, paid.entry)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid.entry, "status"), "Voided")

	def test_r3_9_a_payment_works_out_what_is_owed_once(self):
		pr = self._purchase([("GP-AMMO", 2, 10)])
		with patch.object(cd, "goods_payout_due", wraps=cd.goods_payout_due) as due, \
				patch.object(cd, "_goods_receipt", wraps=cd._goods_receipt) as read:
			cd.record_goods_payout(pr.name, "Cash")
		self.assertEqual((due.call_count, read.call_count), (1, 1))


	# ------------------------------------------------------------------ payment-scope and return cases

	def test_r4_2_a_gun_the_payment_left_out_is_not_covered_by_it_after_its_own_payout_is_undone(self):
		"""The receipt's payment left the gun out (paid on its own then, named in the memo); undoing the
		gun's own payout does not make the receipt's payment cover it."""
		pr, serial, acq = self._gun_receipt(rate=500, extra=[("GP-AMMO", 1, 20)])
		own = cd._new_entry(entry_type="Payout", company=self.company, amount=500, payment_method="Cash", acquisition=acq,
			serial=serial)
		self.assertEqual(cd.record_goods_payout(pr.name, "Cash").amount, 20)
		cd.void_entry(own.entry)
		self.assertIsNone(cd.seller_payments_by_serial([serial])[serial].goods)
		self.assertIsNone(cd.goods_payout_cover(serial))

	def test_r4_3_a_cost_correction_waits_while_the_receipt_payment_is_undone(self):
		"""With the receipt's payment undone, a "paid a different amount" cost correction would book nothing
		and be lost when the payment is recorded again at the receipt's price -- it is refused until then."""
		pr, serial, _acq = self._gun_receipt(rate=500)
		paid = cd.record_goods_payout(pr.name, "Cash")
		cd.void_entry(paid.entry)
		with self.assertRaises(frappe.ValidationError):
			cd.adjust_payout_for_cost_correction(serial, 450)
		self._as(self.manager, api.record_goods_payout, pr.name, "Cash")
		self.assertTrue(cd.adjust_payout_for_cost_correction(serial, 450))

	def test_r4_4_goods_returned_to_the_seller_are_not_owed(self):
		"""Three boxes at $10, one given back on a purchase return: the seller is owed $20, listed and paid."""
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return

		pr = self._purchase([("GP-AMMO", 3, 10)])
		ret = make_purchase_return(pr.name)
		for row in ret.items:
			row.qty = -1
			row.received_qty = -1
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		pr.reload()
		self.assertEqual(cd.goods_payout_due(pr).amount, 20)
		listed = {u["purchase_receipt"]: u["amount"] for u in self._as(self.manager, api.get_today, self.company)[
			"unpaid_goods_receipts"]}
		self.assertEqual(listed.get(pr.name), 20.0)
		self.assertEqual(self._as(self.manager, api.record_goods_payout, pr.name, "Cash").amount, 20)

	def test_r4_7_a_cashiers_page_does_not_build_the_managers_unpaid_list(self):
		self._purchase([("GP-AMMO", 2, 10)])
		with patch.object(cd, "receipts_to_pay", side_effect=AssertionError("managers only")):
			self.assertEqual(self._as(self.clerk, api.get_today, self.company)["unpaid_goods_receipts"], [])
		self.assertTrue(self._as(self.manager, api.get_today, self.company)["unpaid_goods_receipts"])


StoreAGoodsPayout = _store_tests("StoreAGoodsPayout", "store_a", GoodsFixtures, GoodsCases)
StoreBGoodsPayout = _store_tests("StoreBGoodsPayout", "store_b", GoodsFixtures, GoodsCases)
